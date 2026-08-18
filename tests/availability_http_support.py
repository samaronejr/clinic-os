from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from apps.identity.models import Clinic, UserClinicRole
from apps.intake.services import create_patient
from apps.scheduling.models import AvailabilityBlock
from apps.scheduling.services import (
    AppointmentLocalRange,
    create_appointment,
    create_availability,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

CREATED_EVENT: Final = "scheduling.availability.created"
RETIRED_EVENT: Final = "scheduling.availability.retired"
VIEWED_EVENT: Final = "scheduling.availability.viewed"
FUTURE_DATE: Final = "2031-03-04"


@dataclass(frozen=True, slots=True)
class LocalWindow:
    """One clinic-local availability window expressed exactly as submitted."""

    start_time: str
    end_time: str
    local_date: str = FUTURE_DATE


MORNING: Final = LocalWindow("08:00", "09:00")
ADJACENT: Final = LocalWindow("09:00", "10:00")


def availability_list_url(clinic_id: UUID) -> str:
    return f"/scheduling/clinics/{clinic_id}/availability/"


def availability_retire_url(clinic_id: UUID, availability_id: UUID) -> str:
    return f"/scheduling/clinics/{clinic_id}/availability/{availability_id}/retire/"


def create_form_payload(
    practitioner_id: UUID,
    window: LocalWindow,
    idempotency_key: UUID | None = None,
) -> dict[str, str]:
    return {
        "practitioner": str(practitioner_id),
        "local_date": window.local_date,
        "start_time": window.start_time,
        "end_time": window.end_time,
        "idempotency_key": str(idempotency_key or uuid4()),
    }


def grant_role(
    graph: RbacGraph,
    user_id: UUID,
    clinic_id: UUID,
    role: UserClinicRole.Role,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.create(
            user_id=user_id,
            organization_id=graph.organization_a,
            clinic_id=clinic_id,
            role=role,
        )


def seed_block(
    graph: RbacGraph,
    actor: UUID,
    clinic_id: UUID,
    practitioner_id: UUID,
    window: LocalWindow = MORNING,
) -> AvailabilityBlock:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return create_availability(
            clinic_id=clinic_id,
            practitioner_id=practitioner_id,
            start_local=f"{window.local_date}T{window.start_time}",
            end_local=f"{window.local_date}T{window.end_time}",
            idempotency_key=uuid4(),
        )


def active_block_ids(graph: RbacGraph, actor: UUID, clinic_id: UUID) -> list[UUID]:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return [
            row.pk
            for row in AvailabilityBlock.objects.filter(
                clinic_id=clinic_id,
                retired_at__isnull=True,
            ).order_by("start_at", "pk")
        ]


def block_is_retired(graph: RbacGraph, actor: UUID, availability_id: UUID) -> bool:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return AvailabilityBlock.objects.get(pk=availability_id).retired_at is not None


def book_inside_block(
    graph: RbacGraph,
    actor: UUID,
    clinic_id: UUID,
    practitioner_id: UUID,
    window: LocalWindow = MORNING,
) -> None:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        registration = create_patient(
            clinic_id=clinic_id,
            full_name="Nina Synthetic Testpatient",
            birth_date=date(1988, 4, 5),
            idempotency_key=uuid4(),
        )
        create_appointment(
            clinic_id=clinic_id,
            enrollment_id=registration.enrollment.pk,
            practitioner_id=practitioner_id,
            local_range=AppointmentLocalRange(
                start_local=f"{window.local_date}T{window.start_time}",
                end_local=f"{window.local_date}T{window.end_time}",
            ),
            idempotency_key=uuid4(),
        )


def create_dst_clinic(graph: RbacGraph, manager_id: UUID, physician_id: UUID) -> UUID:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        clinic = Clinic.objects.create(
            organization_id=graph.organization_a,
            name="Todo 15 Daylight Clinic",
            crm_uf="SP",
            timezone="America/New_York",
        )
        UserClinicRole.objects.create(
            user_id=manager_id,
            organization_id=graph.organization_a,
            clinic_id=clinic.pk,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
        UserClinicRole.objects.create(
            user_id=physician_id,
            organization_id=graph.organization_a,
            clinic_id=clinic.pk,
            role=UserClinicRole.Role.PHYSICIAN,
        )
    return clinic.pk
