"""Committed release records for the todo-55 authority census.

The guard actor is on a new patient's care team, places the order, files the
result, acknowledges it and approves the clinic's notice through the real
services, so a control release reaches every authority decision of
``release_result_to_patient`` on data production code would leave behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.identity.models import CareTeamMembership
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.orders.services import (
    IdentityEvidence,
    acknowledge_result,
    ingest_result,
    place_order,
)
from apps.workflows.release import publish_release_notice
from django.utils import timezone

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import World, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph
    from workflows.guard_cases import GuardWorld

NOTICE = "Seu resultado está disponível no portal da clínica."
_NAME = "Sintetico Release Census"
_BORN = date(1991, 2, 3)


@dataclass(frozen=True)
class CoordinationWorld:
    """Primary keys only: an acknowledged version and the current notice."""

    version: UUID
    notice_version: int


def seed_coordination_world(world: GuardWorld) -> CoordinationWorld:
    return seed_release(world.graph, world.actor, world.clinic)


def seed_release(graph: RbacGraph, actor: UUID, clinic: UUID) -> CoordinationWorld:
    """Seed the release records for ``actor`` in ``clinic``; no appointment."""
    now = timezone.now()
    with owner_context(graph.organization_a):
        patient = Patient.objects.create(
            organization_id=graph.organization_a, full_name=_NAME, birth_date=_BORN
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"r" * 32,
        )
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient_enrollment=enrollment,
            user_id=actor,
            role="physician",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
    identity = IdentityEvidence(birth_date=_BORN, name=_NAME)
    with as_actor(graph, actor):
        order = place_order(
            clinic_id=clinic,
            enrollment_id=enrollment.pk,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
        receipt = ingest_result(
            clinic_id=clinic,
            submission=submission(World(order, identity, enrollment.pk)),
        )
        acknowledge_result(
            clinic_id=clinic, version_id=receipt.version_id, disposition="no_action"
        )
        notice = publish_release_notice(clinic_id=clinic, body=NOTICE)
    return CoordinationWorld(receipt.version_id, notice.version)
