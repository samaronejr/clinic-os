"""Race-safe idempotent appointment creation service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentPractitionerError,
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
from apps.scheduling.resource_booking import (
    authorized_service_clinic,
    booking_selection,
    service_practitioners,
    validate_resource_window,
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
    clinic = _request_clinic(request)
    prepared = prepare_appointment(clinic, request)
    replay = replay_appointment(
        clinic.organization_id,
        request.idempotency_key,
        prepared.fingerprint,
    )
    if replay is not None:
        return clinic, prepared, replay
    validate_new_appointment(request, prepared)
    _require_practitioner(request)
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


def _request_clinic(request: CreateAppointmentRequest) -> Clinic:
    if request.service_type_id is not None:
        return authorized_service_clinic(request.clinic_id, request.practitioner_id)
    return authorized_appointment_clinic(request.clinic_id)


def _require_practitioner(request: CreateAppointmentRequest) -> None:
    if request.service_type_id is not None:
        if request.practitioner_id not in {
            pk for pk, _ in service_practitioners(request.clinic_id)
        }:
            raise AppointmentPractitionerError
    else:
        require_active_practitioner(request.clinic_id, request.practitioner_id)


@dataclass(frozen=True, slots=True)
class ServiceBooking:
    """Explicit local interval and selected immutable service requirements."""

    local_range: AppointmentLocalRange
    service_type_id: UUID
    resource_ids: tuple[UUID, ...] = ()


def create_service_appointment(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    practitioner_id: UUID,
    booking: ServiceBooking,
    idempotency_key: UUID,
) -> Appointment:
    """Book a service while retaining the legacy create signature and defaults."""
    return _create_appointment(
        CreateAppointmentRequest(
            clinic_id,
            enrollment_id,
            practitioner_id,
            booking.local_range,
            idempotency_key,
            booking.service_type_id,
            booking.resource_ids,
        )
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
    return _create_appointment(
        CreateAppointmentRequest(
            clinic_id,
            enrollment_id,
            practitioner_id,
            local_range,
            idempotency_key,
        )
    )


def _create_appointment(request: CreateAppointmentRequest) -> Appointment:
    clinic_id = request.clinic_id
    practitioner_id = request.practitioner_id
    service_type_id = request.service_type_id
    resource_ids = request.resource_ids
    validate_appointment_syntax(
        request.local_range.start_local, request.local_range.end_local
    )
    with transaction.atomic():
        clinic = _request_clinic(request)
        booking_selection(
            clinic=clinic, service_type_id=service_type_id, resource_ids=resource_ids
        )
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
            resource_ids=resource_ids,
        )
        acquire_appointment_write_gates(target=target)
        clinic = _request_clinic(request)
        prepared = prepare_appointment(clinic, request)
        replay = _replay_for_request(clinic, request, prepared)
        if replay is not None:
            return replay
        validate_new_appointment(request, prepared)
        _require_practitioner(request)
        selection = booking_selection(
            clinic=clinic, service_type_id=service_type_id, resource_ids=resource_ids
        )
        validate_resource_window(
            clinic=clinic,
            practitioner_id=practitioner_id,
            selection=selection,
            interval=(prepared.start_at, prepared.end_at),
        )
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
