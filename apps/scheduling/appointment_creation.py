"""Race-safe idempotent appointment creation service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

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
    patient_booking_requires_approval,
    patient_booking_scope,
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
    if patient_booking_scope() is None and (
        request.service_type_id is not None or request.permission_authority
    ):
        return authorized_service_clinic(request.clinic_id, request.practitioner_id)
    return authorized_appointment_clinic(request.clinic_id)


def _require_practitioner(request: CreateAppointmentRequest) -> None:
    if patient_booking_scope() is None and (
        request.service_type_id is not None or request.permission_authority
    ):
        if request.practitioner_id not in {
            pk for pk, _ in service_practitioners(request.clinic_id)
        }:
            raise AppointmentPractitionerError
    else:
        require_active_practitioner(request.clinic_id, request.practitioner_id)


CREATED_EVENTS: Final = {
    "scheduled": "scheduling.appointment.created",
    "held": "scheduling.appointment.held",
    "requested": "scheduling.appointment.requested",
}


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


def create_hold(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    practitioner_id: UUID,
    local_range: AppointmentLocalRange,
    idempotency_key: UUID,
) -> Appointment:
    """Create an expiring hold (new -> held) for scheduling staff or a patient.

    The database assigns the deadline from the clinic's hold TTL (default 10
    minutes); ``book`` must confirm before it, and expiry frees the slot.
    """
    return _create_appointment(
        CreateAppointmentRequest(
            clinic_id,
            enrollment_id,
            practitioner_id,
            local_range,
            idempotency_key,
            initial_status="held",
            permission_authority=True,
        )
    )


def create_series_occurrence(
    request: CreateAppointmentRequest, *, series_id: UUID, series_index: int
) -> Appointment:
    """Book one materialized series occurrence through the shared write path."""
    return _create_appointment(
        replace(
            request,
            permission_authority=True,
            series_id=series_id,
            series_index=series_index,
        )
    )


def create_patient_booking(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    practitioner_id: UUID,
    local_range: AppointmentLocalRange,
    idempotency_key: UUID,
) -> Appointment:
    """Book (or, under the approval policy, request) one patient-chosen slot."""
    requires_approval = patient_booking_requires_approval()
    return _create_appointment(
        CreateAppointmentRequest(
            clinic_id,
            enrollment_id,
            practitioner_id,
            local_range,
            idempotency_key,
            initial_status="requested" if requires_approval else "scheduled",
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
            CREATED_EVENTS[request.initial_status],
            clinic_id=clinic_id,
            affected_record_id=appointment.pk,
        )
        return appointment
