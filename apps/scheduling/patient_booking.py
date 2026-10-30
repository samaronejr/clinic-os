"""Patient-only facade over the existing idempotent scheduling services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import DatabaseError, connection, transaction
from psycopg import Error as PostgreSQLError

from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_creation import (
    ServiceBooking,
    create_patient_booking,
    create_patient_service_booking,
)
from apps.scheduling.appointment_errors import AppointmentCreateInputError
from apps.scheduling.appointment_lifecycle import (
    AppointmentLifecycleError,
    cancel,
)
from apps.scheduling.models import Appointment, ServiceType
from apps.scheduling.patient_authority import (
    patient_booking_requires_approval,
    require_patient_booking_scope,
)
from apps.scheduling.services import (
    AppointmentLocalRange,
    AppointmentTerminalError,
    cancel_appointment,
    reschedule_appointment,
)

if TYPE_CHECKING:
    from datetime import date, datetime

SLOT_SALT = "scheduling.patient-slot.v1"
PATIENT_CANCEL_NAMESPACE = UUID("0c8f7d52-8a8e-4f7b-9b1b-5d1b3f0a9e61")


@dataclass(frozen=True, slots=True)
class PatientSlot:
    """Only the information required to choose a clinic-local appointment."""

    token: str
    practitioner: str
    start_at: datetime
    end_at: datetime
    service_type_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PatientService:
    """One active service type of the session clinic a patient may book."""

    service_type_id: UUID
    name: str
    duration_min: int


def patient_services() -> tuple[PatientService, ...]:
    """List the session clinic's active service types, read under patient RLS.

    A clinic that reviews self-bookings offers none: its patient rows are born
    ``requested``, and todo 22 gave requested service bookings no edges, so the
    database refuses them. The portal then shows no choice it cannot honour.
    """
    scope = require_patient_booking_scope()
    if patient_booking_requires_approval():
        return ()
    return tuple(
        PatientService(row.pk, row.name, row.duration_min)
        for row in ServiceType.objects.filter(
            organization_id=scope.organization_id,
            clinic_id=scope.clinic_id,
            active=True,
        ).order_by("name", "pk")
    )


def patient_slots(
    day: date,
    appointment_id: UUID | None = None,
    *,
    service_type_id: UUID | None = None,
) -> tuple[PatientSlot, ...]:
    """Project free slots, never other patients' appointments or identities.

    A service type sizes the slots by its duration and buffers and binds one
    free resource of every kind it requires. Rescheduling keeps the generic
    slots; an unknown, retired or foreign service is the same denial.
    """
    scope = require_patient_booking_scope()
    if appointment_id is not None:
        patient_appointment(appointment_id)
    if service_type_id is None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.patient_booking_slots(%s, %s)",
                [day, appointment_id],
            )
            rows = cursor.fetchall()
    else:
        if appointment_id is not None or service_type_id not in {
            service.service_type_id for service in patient_services()
        }:
            raise AppointmentAccessDeniedError
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM clinic_app.patient_service_slots(%s, %s)",
                    [day, service_type_id],
                )
                rows = cursor.fetchall()
        except DatabaseError as error:
            cause = error.__cause__
            if isinstance(cause, PostgreSQLError) and cause.sqlstate == "42501":
                raise AppointmentAccessDeniedError from error
            raise
    zone = ZoneInfo(scope.timezone)
    slots = []
    for practitioner_id, label, start, end, *resources in rows:
        value: dict[str, object] = {
            "session": str(scope.session_id),
            "practitioner": str(practitioner_id),
            "start": start.astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
            "end": end.astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
        }
        if service_type_id is not None:
            value["service"] = str(service_type_id)
            value["resources"] = sorted(str(pk) for pk in resources[0])
        slots.append(
            PatientSlot(
                token=signing.dumps(value, salt=SLOT_SALT),
                practitioner=label,
                start_at=start.astimezone(zone),
                end_at=end.astimezone(zone),
                service_type_id=service_type_id,
            )
        )
    return tuple(slots)


def _slot(
    token: str,
) -> tuple[UUID, AppointmentLocalRange, ServiceBooking | None]:
    scope = require_patient_booking_scope()
    try:
        value = signing.loads(token, salt=SLOT_SALT, max_age=8 * 60 * 60)
        if value["session"] != str(scope.session_id):
            raise AppointmentAccessDeniedError
        local_range = AppointmentLocalRange(value["start"], value["end"])
        booking = (
            ServiceBooking(
                local_range,
                UUID(value["service"]),
                tuple(UUID(pk) for pk in value["resources"]),
            )
            if "service" in value
            else None
        )
        return UUID(value["practitioner"]), local_range, booking
    except (signing.BadSignature, ValueError, KeyError, TypeError) as error:
        raise AppointmentCreateInputError from error


def book_patient_slot(*, token: str, idempotency_key: UUID) -> Appointment:
    """Derive clinic and enrollment from authority, recheck eligibility on write."""
    scope = require_patient_booking_scope()
    practitioner_id, local_range, booking = _slot(token)
    if booking is not None:
        # A service retired, or a policy changed, since the slot was offered:
        # the token no longer names a bookable service.
        if booking.service_type_id not in {
            service.service_type_id for service in patient_services()
        }:
            raise AppointmentCreateInputError
        return create_patient_service_booking(
            clinic_id=scope.clinic_id,
            enrollment_id=scope.enrollment_id,
            practitioner_id=practitioner_id,
            booking=booking,
            idempotency_key=idempotency_key,
        )
    return create_patient_booking(
        clinic_id=scope.clinic_id,
        enrollment_id=scope.enrollment_id,
        practitioner_id=practitioner_id,
        local_range=local_range,
        idempotency_key=idempotency_key,
    )


def patient_appointments() -> tuple[Appointment, ...]:
    """List only this enrollment's clinic appointments under patient RLS."""
    scope = require_patient_booking_scope()
    return tuple(
        Appointment.objects.select_related("service_type")
        .filter(
            organization_id=scope.organization_id,
            clinic_id=scope.clinic_id,
            patient_id=scope.patient_id,
        )
        .order_by("start_at", "pk")
    )


def patient_appointment(appointment_id: UUID) -> Appointment:
    """Reject another patient's opaque identifier without disclosing existence."""
    scope = require_patient_booking_scope()
    try:
        return Appointment.objects.get(
            pk=appointment_id,
            organization_id=scope.organization_id,
            clinic_id=scope.clinic_id,
            patient_id=scope.patient_id,
        )
    except Appointment.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error


def cancel_patient_appointment(appointment_id: UUID) -> Appointment:
    """Keep cancellation terminal and same-reason retries idempotent."""
    appointment = patient_appointment(appointment_id)
    if appointment.status in {
        Appointment.Status.REQUESTED,
        Appointment.Status.HELD,
    }:
        # A withdrawn request or released hold: deterministic command per
        # revision, so a double submit replays instead of conflicting.
        try:
            return cancel(
                clinic_id=appointment.clinic_id,
                appointment_id=appointment.pk,
                expected_revision=appointment.revision,
                command_id=uuid5(PATIENT_CANCEL_NAMESPACE, f"{appointment.pk}"),
            )
        except AppointmentLifecycleError as error:
            raise AppointmentTerminalError from error
    return cancel_appointment(
        appointment_id=appointment.pk,
        reason=Appointment.CancellationReason.PATIENT_REQUEST,
    )


def reschedule_patient_appointment(appointment_id: UUID, token: str) -> Appointment:
    """Move in place, retaining the assigned practitioner and terminal rules."""
    appointment = patient_appointment(appointment_id)
    practitioner_id, local_range, booking = _slot(token)
    if booking is not None or appointment.practitioner_id != practitioner_id:
        raise AppointmentAccessDeniedError
    return reschedule_appointment(
        appointment_id=appointment.pk, local_range=local_range
    )
