"""Real-role duplicate fixtures and verified framework sessions."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from types import ModuleType

    from apps.intake.models import DuplicateCandidate, MergeDecision
    from apps.intake.patient_creation import PatientRegistration

    from rbac_fixtures import RbacGraph


def _pair(
    graph: RbacGraph, services: ModuleType
) -> tuple[PatientRegistration, PatientRegistration, DuplicateCandidate]:
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        registrations = tuple(
            services.create_patient(
                clinic_id=graph.clinic_a,
                full_name=f"Synthetic retained identity {index}",
                birth_date=date(1990 + index, 1, 1),
                idempotency_key=uuid4(),
            )
            for index in range(2)
        )
        for registration in registrations:
            services.add_identifier(
                clinic_id=graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                kind="cpf",
                value="52998224725",
            )
        candidates = services.duplicate_candidates(clinic_id=graph.clinic_a)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    return registrations[0], registrations[1], candidates[0]


def _merged_pair(
    graph: RbacGraph, services: ModuleType
) -> tuple[PatientRegistration, PatientRegistration, MergeDecision, str]:
    first, second, candidate = _pair(graph, services)
    client = verified_client(graph)
    key = session_key(client)
    with (
        runtime_role(),
        tenant_context(graph.clinic_admin, graph.organization_a),
        services.duplicate_review_session(clinic_id=graph.clinic_a, session_key=key),
    ):
        decision = services.adjudicate_duplicate(
            clinic_id=graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic reviewed pair",
        )
    return first, second, decision, key
