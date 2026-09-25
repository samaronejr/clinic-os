"""Synthetic authorization fixtures; no authority is mocked."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    User,
    UserClinicRole,
)
from apps.intake.models import Patient, PatientClinicEnrollment
from django.db import connection, transaction
from django.utils import timezone

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from rbac_fixtures import RbacGraph


@contextmanager
def owner_context(organization_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        yield


@contextmanager
def permission_context(graph: RbacGraph, actor: UUID) -> Iterator[None]:
    # Explicit GUC setup also tests untrusted/sessionless identities: unlike
    # tenant_context this does not pre-reject a persona without staff membership.
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(graph.organization_a), str(actor)],
        )
        yield


def permission_actor(graph: RbacGraph, role: str) -> tuple[UUID, UUID]:
    user = User.objects.create(username=f"synthetic-permission-{uuid4().hex}")
    with owner_context(graph.organization_a):
        if role in UserClinicRole.Role.values:
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=user,
                role=role,
            )
        patient = Patient.objects.create(
            organization_id=graph.organization_a,
            full_name="Sintetico Permission",
            birth_date=date(1990, 1, 1),
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
        council = {"physician": "CRM", "nurse": "COREN", "allied_professional": "CRP"}
        if role in council:
            ProfessionalRegistration.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=user,
                role=role,
                council=council[role],
                number="SINTETICO-001",
                jurisdiction="SP",
                specialty="Sintetico",
                status="regular",
                valid_from=timezone.now() - timedelta(days=1),
                valid_to=timezone.now() + timedelta(days=1),
            )
            CareTeamMembership.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_enrollment=enrollment,
                user=user,
                role=role,
                valid_from=timezone.now() - timedelta(days=1),
                valid_to=timezone.now() + timedelta(days=1),
            )
    return user.pk, enrollment.pk
