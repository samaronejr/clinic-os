"""Patient-only facade over the existing idempotent scheduling services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import connection

from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_errors import AppointmentCreateInputError
from apps.scheduling.models import Appointment
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.services import (
    AppointmentLocalRange,
    cancel_appointment,
    create_appointment,
    reschedule_appointment,
)

if TYPE_CHECKING:
    from datetime import date, datetime

SLOT_SALT = "scheduling.patient-slot.v1"


@dataclass(frozen=True, slots=True)
class PatientSlot:
    """Only the information required to choose a clinic-local appointment."""

    token: str
    practitioner: str
    start_at: datetime
    end_at: datetime


def patient_slots(
    day: date, appointment_id: UUID | None = None
) -> tuple[PatientSlot, ...]:
    """Project free slots, never other patients' appointments or identities."""
    scope = require_patient_booking_scope()
    if appointment_id is not None:
        patient_appointment(appointment_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.patient_booking_slots(%s, %s)",
            [day, appointment_id],
        )
        rows = cursor.fetchall()
    zone = ZoneInfo(scope.timezone)
    return tuple(
        PatientSlot(
            token=signing.dumps(
                {
                    "session": str(scope.session_id),
                    "practitioner": str(practitioner_id),
                    "start": start.astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
                    "end": end.astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
                },
                salt=SLOT_SALT,
            ),
            practitioner=label,
            start_at=start.astimezone(zone),
            end_at=end.astimezone(zone),
        )
        for practitioner_id, label, start, end in rows
    )


def _slot(token: str) -> tuple[UUID, AppointmentLocalRange]:
    scope = require_patient_booking_scope()
    try:
        value = signing.loads(token, salt=SLOT_SALT, max_age=8 * 60 * 60)
        if value["session"] != str(scope.session_id):
            raise AppointmentAccessDeniedError
        return UUID(value["practitioner"]), AppointmentLocalRange(
            value["start"], value["end"]
        )
    except (signing.BadSignature, ValueError, KeyError, TypeError) as error:
        raise AppointmentCreateInputError from error


def book_patient_slot(*, token: str, idempotency_key: UUID) -> Appointment:
    """Derive clinic and enrollment from authority, recheck eligibility on write."""
    scope = require_patient_booking_scope()
    practitioner_id, local_range = _slot(token)
    return create_appointment(
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
        Appointment.objects.filter(
            organization_id=scope.organization_id,
            clinic_id=scope.clinic_id,
            patient_id=scope.patient_id,
        ).order_by("start_at", "pk")
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
    return cancel_appointment(
        appointment_id=appointment.pk,
        reason=Appointment.CancellationReason.PATIENT_REQUEST,
    )


def reschedule_patient_appointment(appointment_id: UUID, token: str) -> Appointment:
    """Move in place, retaining the assigned practitioner and terminal rules."""
    appointment = patient_appointment(appointment_id)
    practitioner_id, local_range = _slot(token)
    if appointment.practitioner_id != practitioner_id:
        raise AppointmentAccessDeniedError
    return reschedule_appointment(
        appointment_id=appointment.pk, local_range=local_range
    )
