"""Explicit patient authority for the shared scheduling write services.

No staff actor or tenant setting is installed. Database resolvers revalidate
sessions; ordinary appointment queries remain restricted to the bound patient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection

from apps.audit.services import record_phase1_event
from apps.identity.models import Clinic
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    authorized_appointment_manager_clinic,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PatientBookingScope:
    """Validated database binding, never caller-selected identities."""

    session_id: UUID
    organization_id: UUID
    clinic_id: UUID
    patient_id: UUID
    enrollment_id: UUID
    clinic_name: str
    timezone: str


def patient_booking_scope() -> PatientBookingScope | None:
    """Return a validated booking authority, or None only for non-patient work."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_patient_session', true), '')"
        )
        setting = cursor.fetchone()
        if setting is None or setting[0] is None:
            return None
        cursor.execute("SELECT * FROM clinic_app.patient_booking_scope()")
        row = cursor.fetchone()
    if row is None:
        raise AppointmentAccessDeniedError
    return PatientBookingScope(*row)


def require_patient_booking_scope() -> PatientBookingScope:
    """Reject staff and unauthenticated callers of the patient facade."""
    scope = patient_booking_scope()
    if scope is None:
        raise AppointmentAccessDeniedError
    return scope


def authorized_appointment_clinic(clinic_id: UUID) -> Clinic:
    """Authorize either the existing manager or an exact patient clinic."""
    scope = patient_booking_scope()
    if scope is None:
        return authorized_appointment_manager_clinic(clinic_id)
    if scope.clinic_id != clinic_id:
        raise AppointmentAccessDeniedError
    return Clinic(
        id=scope.clinic_id,
        organization_id=scope.organization_id,
        name=scope.clinic_name,
        timezone=scope.timezone,
    )


def patient_enrollment(
    clinic: Clinic, enrollment_id: UUID
) -> PatientClinicEnrollment | None:
    """Resolve the only enrollment a patient may submit, without staff RLS."""
    scope = patient_booking_scope()
    if scope is None:
        return None
    if (clinic.pk, clinic.organization_id, enrollment_id) != (
        scope.clinic_id,
        scope.organization_id,
        scope.enrollment_id,
    ):
        raise AppointmentAccessDeniedError
    return PatientClinicEnrollment(
        id=scope.enrollment_id,
        organization_id=scope.organization_id,
        clinic_id=scope.clinic_id,
        patient_id=scope.patient_id,
    )


def patient_practitioner_active(clinic_id: UUID, practitioner_id: UUID) -> bool | None:
    """Check only an eligible physician in the session's clinic."""
    scope = patient_booking_scope()
    if scope is None:
        return None
    if clinic_id != scope.clinic_id:
        raise AppointmentAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.patient_booking_practitioner(%s)", [practitioner_id]
        )
        row = cursor.fetchone()
    return row is not None and row[0] is True


def lock_patient_availability(
    practitioner_ids: tuple[UUID, ...], ranges: tuple[tuple[datetime, datetime], ...]
) -> tuple[UUID, ...]:
    """Lock only containing clinic availability without granting patient UPDATE."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.patient_booking_lock_availability(%s, %s, %s)",
            [list(practitioner_ids), [r[0] for r in ranges], [r[1] for r in ranges]],
        )
        return tuple(row[0] for row in cursor.fetchall())


def record_appointment_event(
    event_type: str, *, clinic_id: UUID, affected_record_id: UUID
) -> None:
    """Keep staff audit unchanged; patient writes receive database-owned receipts."""
    if patient_booking_scope() is None:
        record_phase1_event(
            event_type, clinic_id=clinic_id, affected_record_id=affected_record_id
        )
