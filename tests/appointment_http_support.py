from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from apps.identity.models import Clinic, UserClinicRole
from apps.intake.services import create_patient
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentLocalRange,
    create_appointment,
)
from apps.scheduling.timezones import format_local_minute
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from availability_http_support import FUTURE_DATE
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

BOOKING_VIEWED_EVENT: Final = "scheduling.booking.viewed"
APPOINTMENT_VIEWED_EVENT: Final = "scheduling.appointment.viewed"
APPOINTMENT_CREATED_EVENT: Final = "scheduling.appointment.created"
APPOINTMENT_RESCHEDULED_EVENT: Final = "scheduling.appointment.rescheduled"
APPOINTMENT_CANCELLED_EVENT: Final = "scheduling.appointment.cancelled"
AGENDA_VIEWED_EVENT: Final = "scheduling.agenda.viewed"

INSIDE_START: Final = f"{FUTURE_DATE}T08:15"
INSIDE_END: Final = f"{FUTURE_DATE}T08:45"
BOUNDARY_START: Final = f"{FUTURE_DATE}T08:00"
BOUNDARY_END: Final = f"{FUTURE_DATE}T09:00"
OUTSIDE_START: Final = f"{FUTURE_DATE}T07:30"
OUTSIDE_END: Final = f"{FUTURE_DATE}T08:30"
CROSS_MIDNIGHT_START: Final = f"{FUTURE_DATE}T23:30"
CROSS_MIDNIGHT_END: Final = "2031-03-05T00:30"
LATE_WINDOW: Final = ("23:00", "23:59")
SYNTHETIC_PATIENT: Final = "Nina Synthetic Testpatient"


def appointment_create_url(clinic_id: UUID) -> str:
    return f"/scheduling/clinics/{clinic_id}/appointments/new/"


def agenda_url(clinic_id: UUID) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/"


def agenda_at_url(clinic_id: UUID, view: str, day: str, page: int = 1) -> str:
    return f"/scheduling/clinics/{clinic_id}/agenda/{view}/{day}/{page}/"


def reschedule_url(appointment_id: UUID) -> str:
    return f"/scheduling/appointments/{appointment_id}/reschedule/"


def cancel_url(appointment_id: UUID) -> str:
    return f"/scheduling/appointments/{appointment_id}/cancel/"


def prepare_payload(enrollment_id: UUID) -> dict[str, str]:
    return {"mode": "prepare", "enrollment_id": str(enrollment_id)}


def create_payload(
    enrollment_id: UUID,
    practitioner_id: UUID,
    start_local: str = INSIDE_START,
    end_local: str = INSIDE_END,
    idempotency_key: UUID | None = None,
) -> dict[str, str]:
    return {
        "mode": "create",
        "enrollment_id": str(enrollment_id),
        "practitioner": str(practitioner_id),
        "start_local": start_local,
        "end_local": end_local,
        "idempotency_key": str(idempotency_key or uuid4()),
    }


def reschedule_payload(start_local: str, end_local: str) -> dict[str, str]:
    return {"start_local": start_local, "end_local": end_local}


def seed_enrollment(
    graph: RbacGraph,
    actor: UUID,
    clinic_id: UUID,
    full_name: str = SYNTHETIC_PATIENT,
    birth_day: int = 5,
) -> UUID:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        registration = create_patient(
            clinic_id=clinic_id,
            full_name=full_name,
            birth_date=date(1988, 4, birth_day),
            idempotency_key=uuid4(),
        )
    return registration.enrollment.pk


@dataclass(frozen=True, slots=True)
class BookingContext:
    """Bind one synthetic clinic to the manager acting inside it."""

    graph: RbacGraph
    actor: UUID
    clinic_id: UUID


def seed_appointment(
    context: BookingContext,
    enrollment_id: UUID,
    practitioner_id: UUID,
    local_range: tuple[str, str] = (INSIDE_START, INSIDE_END),
) -> Appointment:
    start_local, end_local = local_range
    with (
        runtime_role(),
        tenant_context(context.actor, context.graph.organization_a),
    ):
        return create_appointment(
            clinic_id=context.clinic_id,
            enrollment_id=enrollment_id,
            practitioner_id=practitioner_id,
            local_range=AppointmentLocalRange(
                start_local=start_local,
                end_local=end_local,
            ),
            idempotency_key=uuid4(),
        )


def reload_appointment(
    graph: RbacGraph,
    actor: UUID,
    appointment_id: UUID,
) -> Appointment:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return Appointment.objects.get(pk=appointment_id)


def appointment_rows(
    graph: RbacGraph,
    actor: UUID,
) -> list[Appointment]:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return list(Appointment.objects.all().order_by("start_at", "pk"))


def create_zone_clinic(
    graph: RbacGraph,
    manager_id: UUID,
    physician_id: UUID,
    timezone_key: str,
    name: str = "Todo 16 Zone Clinic",
) -> UUID:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        clinic = Clinic.objects.create(
            organization_id=graph.organization_a,
            name=name,
            crm_uf="SP",
            timezone=timezone_key,
        )
        for user_id, role in (
            (manager_id, UserClinicRole.Role.RECEPTIONIST),
            (physician_id, UserClinicRole.Role.PHYSICIAN),
        ):
            UserClinicRole.objects.create(
                user_id=user_id,
                organization_id=graph.organization_a,
                clinic_id=clinic.pk,
                role=role,
            )
    return clinic.pk


def booked_local_range(
    context: BookingContext,
    appointment: Appointment,
    timezone_key: str = "America/Sao_Paulo",
) -> tuple[str, str]:
    """Return the durable clinic-local minutes this appointment actually holds."""
    del context
    return (
        format_local_minute(appointment.start_at, timezone_key),
        format_local_minute(appointment.end_at, timezone_key),
    )
