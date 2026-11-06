"""Real-role worlds for todo 55: an acknowledged result, delegates, portal sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.comms import inbox_services
from apps.comms.models import IntegrationOperation
from apps.comms.portal_messages import reply_to_portal_conversation
from apps.core.integration import execute_operation
from apps.identity.models import UserClinicRole
from apps.intake.models import Patient
from apps.intake.patient_access import issue_invitation, patient_session_context
from apps.orders.services import (
    IdentityEvidence,
    acknowledge_result,
    ingest_result,
    place_order,
)
from apps.tenancy.db import tenant_context
from apps.workflows.release import publish_release_notice
from apps.workflows.urgent import set_urgent_coverage

from comms.support import manager_of
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import World, submission
from patient_service_support import runtime_role
from patients.delegate_support import (
    DelegateWorld,
    NotesFixture,
    active_grant,
    delegate_world,
)
from renewal.test_patient_access import _issue, _redeem, _seed_patients
from workflows.permission_support import permission_actor

if TYPE_CHECKING:
    from pathlib import Path

    from apps.identity.delegates import DelegateReceipt

    from ehr.longitudinal_support import ChartWorld
    from rbac_fixtures import RbacGraph

NOTICE = "Um novo resultado está disponível no portal da clínica."
URGENT = "Estou com dor no peito muito forte agora e falta de ar."
ROUTINE = "Gostaria de saber o horário de funcionamento da clínica."


@dataclass(frozen=True, slots=True)
class ReleaseWorld:
    graph: RbacGraph
    enrollment: UUID
    version: UUID
    delegate: DelegateWorld
    grant: DelegateReceipt


@dataclass(frozen=True, slots=True)
class UrgentWorld:
    graph: RbacGraph
    enrollment: UUID
    session: UUID
    conversation: UUID
    nurse: UUID
    other_enrollment: UUID


def bundle_v2(graph: RbacGraph) -> None:
    """Release follow-ups and the task feed need the workflow candidate bundle."""
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(organization_id=graph.organization_a).update(
            workflow_bundle_version=2
        )


def acknowledged(graph: RbacGraph) -> tuple[ChartWorld, UUID]:
    """An acknowledged clinic-A result version and an approved notice."""
    chart = chart_world(graph)
    with owner_context(graph.organization_a):
        patient = Patient.objects.get(pk=chart.patient)
        identity = IdentityEvidence(
            birth_date=patient.birth_date, name=patient.full_name
        )
    with as_actor(graph, graph.physician):
        order = place_order(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
        receipt = ingest_result(
            clinic_id=graph.clinic_a,
            submission=submission(World(order, identity, chart.enrollment)),
        )
        acknowledge_result(
            clinic_id=graph.clinic_a,
            version_id=receipt.version_id,
            disposition="no_action",
        )
        publish_release_notice(clinic_id=graph.clinic_a, body=NOTICE)
    return chart, receipt.version_id


def release_world(graph: RbacGraph, root: Path) -> ReleaseWorld:
    """One acknowledged result for a patient with an active delegate grant."""
    chart, version = acknowledged(graph)
    notes = NotesFixture(
        chart.enrollment,
        chart.patient,
        chart.version.document.encounter_id,
        chart.version.pk,
    )
    delegate = delegate_world(graph, root, notes=notes)
    grant = active_grant(graph, delegate)
    bundle_v2(graph)
    return ReleaseWorld(graph, chart.enrollment, version, delegate, grant)


def run_operation(graph: RbacGraph, message: UUID) -> str:
    """Execute the message's outbox operation as the worker does."""
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(subject_id=message)
    with runtime_role():
        return execute_operation(operation.pk)


def urgent_world(
    graph: RbacGraph,
    *,
    roles: tuple[str, ...] = ("nurse",),
    hours: int = 1,
    coverage: bool = True,
) -> UrgentWorld:
    """A patient with a live portal session in a care thread a nurse opened."""
    access = _seed_patients(graph)
    _, secret = _issue(graph, access.enrollment_id)
    session = _redeem(graph.clinic_a, secret)
    assert session is not None
    with as_actor(graph, graph.physician):
        conversation = inbox_services.open_conversation(
            clinic_id=graph.clinic_a,
            enrollment_id=access.enrollment_id,
            channel="portal",
            purpose="transactional",
        ).conversation_id
    if coverage:
        # The graph's own clinic_admin administers clinic B; clinic A needs its
        # own, created before the runtime role takes over the connection.
        manager = manager_of(graph)
        with runtime_role(), tenant_context(manager, graph.organization_a):
            set_urgent_coverage(
                clinic_id=graph.clinic_a,
                expected_version=0,
                weekdays=(0, 1, 2, 3, 4, 5, 6),
                opens_at=time(0, 0),
                closes_at=time(23, 59),
                response_hours=hours,
                responsible_roles=roles,
            )
    nurse, _enrollment = permission_actor(graph, "nurse")
    return UrgentWorld(
        graph,
        access.enrollment_id,
        session,
        conversation,
        nurse,
        access.other_enrollment_id,
    )


def clinic_b_portal(graph: RbacGraph, enrollment: UUID) -> tuple[UUID, UUID]:
    """A live portal session and a staff-opened thread for a clinic-B patient."""
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        issued = issue_invitation(clinic_id=graph.clinic_b, enrollment_id=enrollment)
        thread = inbox_services.open_conversation(
            clinic_id=graph.clinic_b,
            enrollment_id=enrollment,
            channel="portal",
            purpose="transactional",
        ).conversation_id
    session = _redeem(graph.clinic_b, issued.secret)
    assert session is not None
    return session, thread


def patient_reply(world: UrgentWorld, text: str, key: UUID | None = None) -> UUID:
    return reply_as(world.session, world.graph.clinic_a, world.conversation, text, key)


def reply_as(
    session: UUID, clinic: UUID, thread: UUID, text: str, key: UUID | None = None
) -> UUID:
    """Reply in a portal thread under one patient session."""
    with runtime_role(), patient_session_context(session):
        return reply_to_portal_conversation(
            clinic_id=clinic,
            conversation_id=thread,
            body=text,
            idempotency_key=key or uuid4(),
        ).message_id
