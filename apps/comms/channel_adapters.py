"""Three independently gated synthetic reminder contracts; no real transport.

An acceptance reference is not a delivery receipt. Synthetic delivery receipts
are applied separately through the authenticated callback boundary in QA.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar
from zoneinfo import ZoneInfo

from django.conf import settings
from django.template.loader import render_to_string

from apps.comms.adapters import PermanentSendError, SendResult, TransientSendError
from apps.comms.capabilities import channel_capability, template_enabled
from apps.comms.models import AppointmentReminder
from apps.intake.models import PatientContact

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.models import IntegrationOperation


@dataclass(frozen=True, slots=True)
class ReminderMessage:
    """Ephemeral minimal request, never stored or logged."""

    channel: str
    destination: str
    body: str
    template_version: int


class ReminderAdapter:
    """Shared mechanics; each concrete channel retains its own gate and key."""

    channel: ClassVar[str]
    provider: str

    def prepare(self, operation: IntegrationOperation) -> ReminderMessage:
        """Prepare only versioned appointment logistics within tenant context."""
        if operation.channel != self.channel:
            raise PermanentSendError
        reminder = AppointmentReminder.objects.select_related(
            "appointment", "operation__clinic"
        ).get(operation=operation)
        contact = PatientContact.objects.filter(
            patient_id=reminder.appointment.patient_id,
            channel=self.channel,
            destination_version=reminder.contact_version,
            verified_version=reminder.contact_version,
        ).first()
        if contact is None:
            # A destination may change between recheck and preparation.
            raise PermanentSendError
        zone = ZoneInfo(reminder.timezone)
        body = render_to_string(
            f"comms/{self.channel}_reminder_v1.txt",
            {
                "clinic": operation.clinic.name,
                "date": reminder.start_at.astimezone(zone).strftime("%d/%m/%Y"),
                "start": reminder.start_at.astimezone(zone).strftime("%H:%M"),
                "end": reminder.end_at.astimezone(zone).strftime("%H:%M"),
                "timezone": reminder.timezone,
            },
        ).strip()
        return ReminderMessage(
            self.channel, contact.destination, body, reminder.template_version
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return only explicitly synthetic acceptance, or fail closed."""
        if (
            not isinstance(prepared, ReminderMessage)
            or prepared.channel != self.channel
            or not channel_capability(self.channel).synthetic_enabled
            or not template_enabled(self.channel, prepared.template_version)
        ):
            raise PermanentSendError
        # Explicit fault injection only on the already-enabled synthetic path.
        if self.channel in getattr(settings, "COMMS_SYNTHETIC_FAILURE_CHANNELS", ()):
            raise TransientSendError
        return SendResult(f"synthetic:{self.channel}:{operation_id}")


class EmailReminderAdapter(ReminderAdapter):
    """Email logistics contract, separately gated from both phone channels."""

    channel = "email"
    provider = "reminder-email-v1"


class SMSReminderAdapter(ReminderAdapter):
    """SMS logistics contract, separately gated from email and WhatsApp."""

    channel = "sms"
    provider = "reminder-sms-v1"


class WhatsAppReminderAdapter(ReminderAdapter):
    """WhatsApp exact-template contract, independently revocable."""

    channel = "whatsapp"
    provider = "reminder-whatsapp-v1"
