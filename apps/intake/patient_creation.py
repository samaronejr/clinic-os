"""Transaction-bound patient registration service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.core.idempotency import (
    PatientNameValueError,
    create_fingerprint,
    normalize_patient_name,
)
from apps.intake.access import authorized_manager_clinic
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.timezones import format_local_minute

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.models import Clinic


@dataclass(frozen=True, slots=True)
class PatientRegistration:
    """Return the patient identity and its selected-clinic enrollment."""

    patient: Patient
    enrollment: PatientClinicEnrollment


class PatientIdempotencyConflictError(Exception):
    """Reject reuse of a patient-create key for different canonical input."""

    def __init__(self) -> None:
        """Expose one stable non-identifying conflict message."""
        super().__init__("patient registration idempotency conflict")


class PatientBirthDateError(ValueError):
    """Reject a patient birth date outside the locked clinic date."""

    def __init__(self) -> None:
        """Expose one stable non-identifying date message."""
        super().__init__("patient birth date is invalid")


class PatientCreateInputError(ValueError):
    """Reject malformed patient-create input without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("patient create input is invalid")


def _registration_for_key(
    organization_id: UUID,
    idempotency_key: UUID,
    fingerprint: bytes,
) -> PatientRegistration | None:
    enrollment = (
        PatientClinicEnrollment.objects.select_related("patient")
        .filter(
            organization_id=organization_id,
            idempotency_key=idempotency_key,
        )
        .first()
    )
    if enrollment is None:
        return None
    if bytes(enrollment.create_fingerprint) != fingerprint:
        raise PatientIdempotencyConflictError
    return PatientRegistration(patient=enrollment.patient, enrollment=enrollment)


def _normalized_name(full_name: str) -> str:
    try:
        return normalize_patient_name(full_name)
    except PatientNameValueError as error:
        raise PatientCreateInputError from error


def _validate_birth_date(birth_date: date, clinic: Clinic) -> None:
    utc_minute = timezone.now().astimezone(UTC).replace(second=0, microsecond=0)
    timezone_key = clinic.timezone
    if not isinstance(timezone_key, str):
        raise PatientBirthDateError
    clinic_today = date.fromisoformat(
        format_local_minute(utc_minute, timezone_key)[:10]
    )
    if birth_date > clinic_today:
        raise PatientBirthDateError


def create_patient(
    *,
    clinic_id: UUID,
    full_name: str,
    birth_date: date,
    idempotency_key: UUID,
) -> PatientRegistration:
    """Create one normalized organization patient and clinic enrollment."""
    with transaction.atomic():
        clinic = authorized_manager_clinic(clinic_id)
        normalized_name = _normalized_name(full_name)
        fingerprint = create_fingerprint(
            "patient",
            {
                "birth_date": birth_date.isoformat(),
                "clinic_id": str(clinic_id),
                "full_name": normalized_name,
            },
        )
        replay = _registration_for_key(
            clinic.organization_id,
            idempotency_key,
            fingerprint,
        )
        if replay is not None:
            return replay
        acquire_advisory_locks((clinic_lock_key(clinic_id),))
        clinic = authorized_manager_clinic(clinic_id)
        replay = _registration_for_key(
            clinic.organization_id,
            idempotency_key,
            fingerprint,
        )
        if replay is not None:
            return replay
        _validate_birth_date(birth_date, clinic)
        try:
            with transaction.atomic():
                patient = Patient.objects.create(
                    organization_id=clinic.organization_id,
                    full_name=normalized_name,
                    birth_date=birth_date,
                )
                enrollment = PatientClinicEnrollment.objects.create(
                    organization_id=clinic.organization_id,
                    clinic=clinic,
                    patient=patient,
                    idempotency_key=idempotency_key,
                    create_fingerprint=fingerprint,
                )
        except IntegrityError:
            replay = _registration_for_key(
                clinic.organization_id,
                idempotency_key,
                fingerprint,
            )
            if replay is not None:
                return replay
            raise
        record_phase1_event(
            "intake.patient.created",
            clinic_id=clinic_id,
            affected_record_id=patient.pk,
        )
        return PatientRegistration(patient=patient, enrollment=enrollment)
