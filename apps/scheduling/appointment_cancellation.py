"""Locked terminal appointment cancellation service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.scheduling.appointment_errors import (
    AppointmentCancellationConflictError,
    AppointmentCancellationInputError,
)
from apps.scheduling.appointment_locking import (
    acquire_appointment_write_gates,
    lock_appointment_write_rows,
)
from apps.scheduling.appointment_transition_state import (
    discover_transition_appointment,
    reload_transition_appointment,
    transition_write_target,
)
from apps.scheduling.models import Appointment
from apps.scheduling.patient_authority import (
    authorized_appointment_clinic,
    record_appointment_event,
)

if TYPE_CHECKING:
    from uuid import UUID


def cancel_appointment(*, appointment_id: UUID, reason: str) -> Appointment:
    """Cancel one scheduled appointment using only the fixed reason enum."""
    if (
        not isinstance(reason, str)
        or reason not in Appointment.CancellationReason.values
    ):
        raise AppointmentCancellationInputError
    with transaction.atomic():
        discovered = discover_transition_appointment(appointment_id)
        target = transition_write_target(discovered)
        acquire_appointment_write_gates(target=target)
        _ = authorized_appointment_clinic(target.clinic_id)
        current = reload_transition_appointment(target, appointment_id)
        _ = lock_appointment_write_rows(
            target=target,
            start_at=current.start_at,
            end_at=current.end_at,
            appointment_ids=(appointment_id,),
        )
        _ = authorized_appointment_clinic(target.clinic_id)
        current = reload_transition_appointment(
            target,
            appointment_id,
            for_update=True,
        )
        if current.status == Appointment.Status.CANCELLED:
            if current.cancellation_reason == reason:
                return current
            raise AppointmentCancellationConflictError
        current.status = Appointment.Status.CANCELLED
        current.cancellation_reason = reason
        current.cancelled_at = timezone.now()
        current.save(
            update_fields=(
                "status",
                "cancellation_reason",
                "cancelled_at",
                "updated_at",
            )
        )
        record_appointment_event(
            "scheduling.appointment.cancelled",
            clinic_id=target.clinic_id,
            affected_record_id=current.pk,
        )
        return current
