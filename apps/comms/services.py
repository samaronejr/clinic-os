"""Reminder eligibility and immutable logistics at the shared send boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.utils import timezone

from apps.comms.capabilities import template_enabled
from apps.comms.models import AppointmentReminder
from apps.intake.models import PatientContact

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.adapters import OperationScope

SUBJECT_TYPE: Final = "scheduling.appointment_reminder"


def reminder_send_eligible(scope: OperationScope) -> bool:
    """Reject stale appointments, opt-out/re-opt-in and changed destinations."""
    reminder = (
        AppointmentReminder.objects.select_related(
            "appointment", "preference", "operation"
        )
        .filter(operation_id=scope.operation_id)
        .first()
    )
    if reminder is None:
        return False
    appointment, preference, operation = (
        reminder.appointment,
        reminder.preference,
        reminder.operation,
    )
    return (
        appointment.organization_id == scope.organization_id
        and appointment.clinic_id == scope.clinic_id
        and operation.subject_id == appointment.pk
        and appointment.status == "scheduled"
        and appointment.start_at == reminder.start_at
        and appointment.end_at == reminder.end_at
        and appointment.start_at > timezone.now()
        and preference.clinic_id == scope.clinic_id
        and preference.patient_id == appointment.patient_id
        and preference.channel == operation.channel
        and preference.purpose == "appointment_reminder"
        and preference.opted_in
        and preference.version == reminder.preference_version
        and template_enabled(operation.channel, reminder.template_version)
        and PatientContact.objects.filter(
            organization_id=scope.organization_id,
            patient_id=appointment.patient_id,
            channel=operation.channel,
            destination_version=reminder.contact_version,
            verified_version=reminder.contact_version,
        ).exists()
    )


def reminder_lock_key(scope: OperationScope, subject_id: UUID) -> str | None:
    """Share task 14's patient/channel mutation key in addition to appointment."""
    reminder = (
        AppointmentReminder.objects.select_related("preference")
        .filter(operation_id=scope.operation_id, appointment_id=subject_id)
        .first()
    )
    if reminder is None:
        return None
    preference = reminder.preference
    return (
        f"intake.patient_channel_preference:{preference.patient_id}:"
        f"{preference.channel}"
    )
