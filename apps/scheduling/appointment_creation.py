"""Race-safe idempotent appointment creation service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    SlotConflict,
)
from apps.scheduling.appointment_locking import (
    AppointmentWriteRows,
    AppointmentWriteTarget,
    acquire_appointment_write_gates,
    lock_appointment_write_rows,
)
from apps.scheduling.appointment_persistence import insert_appointment
from apps.scheduling.appointment_values import (
    AppointmentLocalRange,
    CreateAppointmentRequest,
    PreparedAppointment,
    prepare_appointment,
    replay_appointment,
    require_active_practitioner,
    validate_appointment_syntax,
    validate_new_appointment,
)
from apps.scheduling.patient_authority import (
    authorized_appointment_clinic,
    record_appointment_event,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.models import Clinic
    from apps.scheduling.models import Appointment


def _post_lock_revalidate(
    request: CreateAppointmentRequest,
    expected: PreparedAppointment,
    rows: AppointmentWriteRows,
) -> tuple[Clinic, PreparedAppointment, Appointment | None]:
    clinic = authorized_appointment_clinic(request.clinic_id)
    prepared = prepare_appointment(clinic, request)
    replay = replay_appointment(
        clinic.organization_id,
        request.idempotency_key,
        prepared.fingerprint,
    )
    if replay is not None:
        return clinic, prepared, replay
    validate_new_appointment(request, prepared)
    require_active_practitioner(request.clinic_id, request.practitioner_id)
    if (
        prepared.enrollment.patient_id != expected.enrollment.patient_id
        or prepared.start_at != expected.start_at
        or prepared.end_at != expected.end_at
        or prepared.fingerprint != expected.fingerprint
        or rows.availability is None
    ):
        raise AppointmentAvailabilityError
    rows.availability.refresh_from_db()
    if (
        rows.availability.retired_at is not None
        or rows.availability.organization_id != clinic.organization_id
        or rows.availability.clinic_id != request.clinic_id
        or rows.availability.practitioner_id != request.practitioner_id
        or rows.availability.start_at > prepared.start_at
        or rows.availability.end_at < prepared.end_at
    ):
        raise AppointmentAvailabilityError
    if rows.conflicting_appointment_ids:
        raise SlotConflict
    return clinic, prepared, None


def _replay_for_request(
    clinic: Clinic,
    request: CreateAppointmentRequest,
    prepared: PreparedAppointment,
) -> Appointment | None:
    return replay_appointment(
        clinic.organization_id,
        request.idempotency_key,
        prepared.fingerprint,
    )


def create_appointment(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    practitioner_id: UUID,
    local_range: AppointmentLocalRange,
    idempotency_key: UUID,
) -> Appointment:
    """Create one manager- or patient-authorized clinic-local booking once."""
    validate_appointment_syntax(local_range.start_local, local_range.end_local)
    request = CreateAppointmentRequest(
        clinic_id,
        enrollment_id,
        practitioner_id,
        local_range,
        idempotency_key,
    )
    with transaction.atomic():
        clinic = authorized_appointment_clinic(clinic_id)
        prepared = prepare_appointment(clinic, request)
        replay = _replay_for_request(clinic, request, prepared)
        if replay is not None:
            return replay
        validate_new_appointment(request, prepared)
        target = AppointmentWriteTarget(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            patient_id=prepared.enrollment.patient_id,
            practitioner_ids=(practitioner_id,),
        )
        acquire_appointment_write_gates(target=target)
        clinic = authorized_appointment_clinic(clinic_id)
        prepared = prepare_appointment(clinic, request)
        replay = _replay_for_request(clinic, request, prepared)
        if replay is not None:
            return replay
        validate_new_appointment(request, prepared)
        require_active_practitioner(clinic_id, practitioner_id)
        rows = lock_appointment_write_rows(
            target=target,
            start_at=prepared.start_at,
            end_at=prepared.end_at,
        )
        clinic, prepared, replay = _post_lock_revalidate(request, prepared, rows)
        if replay is not None:
            return replay
        appointment, created = insert_appointment(clinic, request, prepared)
        if not created:
            return appointment
        record_appointment_event(
            "scheduling.appointment.created",
            clinic_id=clinic_id,
            affected_record_id=appointment.pk,
        )
        return appointment
