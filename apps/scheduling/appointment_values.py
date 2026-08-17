"""Canonical appointment request preparation and replay matching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.core.idempotency import create_fingerprint
from apps.identity.current_context import list_active_clinic_physicians
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_errors import (
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentPractitionerError,
)
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import LOCAL_MINUTE_PATTERN, parse_local_minute

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.identity.models import Clinic


@dataclass(frozen=True, slots=True)
class AppointmentLocalRange:
    """Strict caller-supplied clinic-local minute bounds."""

    start_local: str
    end_local: str


@dataclass(frozen=True, slots=True)
class CreateAppointmentRequest:
    """Immutable caller input retained across every lock revalidation."""

    clinic_id: UUID
    enrollment_id: UUID
    practitioner_id: UUID
    local_range: AppointmentLocalRange
    idempotency_key: UUID


@dataclass(frozen=True, slots=True)
class PreparedAppointment:
    """Canonical clinic-scoped enrollment, UTC range, and fingerprint."""

    enrollment: PatientClinicEnrollment
    start_at: datetime
    end_at: datetime
    fingerprint: bytes


def validate_appointment_syntax(start_local: str, end_local: str) -> None:
    """Reject values outside the exact local-minute surface."""
    if (
        not isinstance(start_local, str)
        or LOCAL_MINUTE_PATTERN.fullmatch(start_local) is None
        or not isinstance(end_local, str)
        or LOCAL_MINUTE_PATTERN.fullmatch(end_local) is None
    ):
        raise AppointmentCreateInputError


def prepare_appointment(
    clinic: Clinic,
    request: CreateAppointmentRequest,
) -> PreparedAppointment:
    """Resolve clinic enrollment and canonical UTC fingerprint."""
    enrollment = _enrollment(clinic, request.enrollment_id)
    try:
        timezone_key = _timezone_key(clinic)
        start_at = parse_local_minute(request.local_range.start_local, timezone_key)
        end_at = parse_local_minute(request.local_range.end_local, timezone_key)
    except ValueError as error:
        raise AppointmentCreateInputError from error
    fingerprint = create_fingerprint(
        "appointment",
        {
            "clinic_id": str(request.clinic_id),
            "end_utc": _utc_minute(end_at),
            "enrollment_id": str(request.enrollment_id),
            "practitioner_id": str(request.practitioner_id),
            "start_utc": _utc_minute(start_at),
        },
    )
    return PreparedAppointment(enrollment, start_at, end_at, fingerprint)


def replay_appointment(
    organization_id: UUID,
    idempotency_key: UUID,
    fingerprint: bytes,
) -> Appointment | None:
    """Return an equal durable replay before mutable scheduling checks."""
    appointment = Appointment.objects.filter(
        organization_id=organization_id,
        idempotency_key=idempotency_key,
    ).first()
    if appointment is None:
        return None
    if bytes(appointment.create_fingerprint) != fingerprint:
        raise AppointmentIdempotencyConflictError
    return appointment


def validate_new_appointment(
    request: CreateAppointmentRequest,
    prepared: PreparedAppointment,
) -> None:
    """Require one positive future clinic-local date range."""
    if (
        request.local_range.start_local[:10] != request.local_range.end_local[:10]
        or prepared.end_at <= prepared.start_at
        or prepared.start_at <= timezone.now()
    ):
        raise AppointmentCreateInputError


def require_active_practitioner(clinic_id: UUID, practitioner_id: UUID) -> None:
    """Require an active exact physician role after advisory gates."""
    physicians = list_active_clinic_physicians(clinic_id)
    if practitioner_id not in {entry.user_id for entry in physicians}:
        raise AppointmentPractitionerError


def _enrollment(clinic: Clinic, enrollment_id: UUID) -> PatientClinicEnrollment:
    try:
        return PatientClinicEnrollment.objects.get(
            organization_id=clinic.organization_id,
            clinic_id=clinic.pk,
            pk=enrollment_id,
        )
    except PatientClinicEnrollment.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error


def _timezone_key(clinic: Clinic) -> str:
    value = clinic.timezone
    if not isinstance(value, str):
        raise AppointmentCreateInputError
    return value


def _utc_minute(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")
