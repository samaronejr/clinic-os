"""Locked in-place appointment rescheduling service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    SlotConflict,
)
from apps.scheduling.appointment_locking import (
    acquire_appointment_write_gates,
    lock_appointment_write_rows,
)
from apps.scheduling.appointment_persistence import update_appointment_range
from apps.scheduling.appointment_transition_state import (
    discover_transition_appointment,
    reload_transition_appointment,
    transition_write_target,
)
from apps.scheduling.appointment_values import (
    AppointmentLocalRange,
    validate_appointment_syntax,
)
from apps.scheduling.models import Appointment
from apps.scheduling.patient_authority import (
    patient_booking_scope,
    record_appointment_event,
)
from apps.scheduling.resource_booking import (
    authorized_transition_clinic,
    booking_selection,
    require_transition_practitioner,
    validate_resource_window,
)
from apps.scheduling.timezones import parse_local_minute

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.identity.models import Clinic


def _parse_range(
    clinic: Clinic,
    local_range: AppointmentLocalRange,
) -> tuple[datetime, datetime]:
    _validate_syntax(local_range)
    timezone_key = clinic.timezone
    if not isinstance(timezone_key, str):
        raise AppointmentRescheduleInputError
    try:
        start_at = parse_local_minute(local_range.start_local, timezone_key)
        end_at = parse_local_minute(local_range.end_local, timezone_key)
    except ValueError as error:
        raise AppointmentRescheduleInputError from error
    if (
        local_range.start_local[:10] != local_range.end_local[:10]
        or end_at <= start_at
        or start_at <= timezone.now()
    ):
        raise AppointmentRescheduleInputError
    return start_at, end_at


def _validate_syntax(local_range: AppointmentLocalRange) -> None:
    try:
        validate_appointment_syntax(local_range.start_local, local_range.end_local)
    except AppointmentCreateInputError as error:
        raise AppointmentRescheduleInputError from error


def reschedule_appointment(
    *,
    appointment_id: UUID,
    local_range: AppointmentLocalRange,
) -> Appointment:
    """Move one scheduled appointment under the shared write-lock order."""
    _validate_syntax(local_range)
    with transaction.atomic():
        discovered = discover_transition_appointment(appointment_id)
        target = transition_write_target(discovered)
        acquire_appointment_write_gates(target=target)
        clinic = authorized_transition_clinic(discovered)
        current = reload_transition_appointment(target, appointment_id)
        start_at, end_at = _parse_range(clinic, local_range)
        rows = lock_appointment_write_rows(
            target=target,
            start_at=current.start_at,
            end_at=current.end_at,
            additional_ranges=((start_at, end_at),),
            appointment_ids=(appointment_id,),
        )
        clinic = authorized_transition_clinic(current)
        current = reload_transition_appointment(
            target,
            appointment_id,
            for_update=True,
        )
        start_at, end_at = _parse_range(clinic, local_range)
        if current.status != Appointment.Status.SCHEDULED:
            raise AppointmentTerminalError
        require_transition_practitioner(current)
        if len(rows.additional_availability) != 1:
            raise AppointmentAvailabilityError
        old_availability = rows.availability
        new_availability = rows.additional_availability[0]
        if old_availability is None or new_availability is None:
            raise AppointmentAvailabilityError
        for availability, range_start, range_end in (
            (old_availability, current.start_at, current.end_at),
            (new_availability, start_at, end_at),
        ):
            if (
                availability.organization_id != target.organization_id
                or availability.clinic_id != target.clinic_id
                or availability.practitioner_id != current.practitioner_id
                or availability.retired_at is not None
                or availability.start_at > range_start
                or availability.end_at < range_end
            ):
                raise AppointmentAvailabilityError
        if patient_booking_scope() is None:
            validate_resource_window(
                clinic=clinic,
                practitioner_id=current.practitioner_id,
                selection=booking_selection(
                    clinic=clinic,
                    service_type_id=current.service_type_id,
                    resource_ids=tuple(current.resource_ids),
                ),
                interval=(start_at, end_at),
                appointment_id=current.pk,
            )
        if any(pk != current.pk for pk in rows.conflicting_appointment_ids):
            raise SlotConflict
        if current.start_at == start_at and current.end_at == end_at:
            return current
        current = update_appointment_range(
            current,
            start_at=start_at,
            end_at=end_at,
        )
        record_appointment_event(
            "scheduling.appointment.rescheduled",
            clinic_id=target.clinic_id,
            affected_record_id=current.pk,
        )
        return current
