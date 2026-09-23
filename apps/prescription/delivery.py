"""Link-only document delivery adapter for the shared comms outbox.

Approved delivery sends the patient a message containing the public
verification URL and portal instructions. Document bytes never enter the
outbox, the message body or an email attachment: the patient retrieves
the signed artifact through the released patient download surface. The
adapter is synthetic-only and independently gated like the reminder
channels; no real delivery provider is approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from apps.comms.adapters import PermanentSendError, SendResult
from apps.comms.capabilities import channel_capability
from apps.intake.models import PatientContact
from apps.prescription.models import PrescriptionDocument
from apps.prescription.verification import DELIVERY_CHANNEL, DELIVERY_PROVIDER

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.models import IntegrationOperation


@dataclass(frozen=True, slots=True)
class DocumentDeliveryMessage:
    """Ephemeral minimal request, never stored or logged."""

    channel: str
    destination: str
    body: str


class DocumentDeliveryAdapter:
    """Prepare inside the tenant transaction; send outside it."""

    provider = DELIVERY_PROVIDER

    def prepare(self, operation: IntegrationOperation) -> DocumentDeliveryMessage:
        """Build the link-only message from stored scope inside the tenant."""
        if operation.channel != DELIVERY_CHANNEL:
            raise PermanentSendError
        document = PrescriptionDocument.objects.filter(pk=operation.subject_id).first()
        if document is None:
            raise PermanentSendError
        contact = (
            PatientContact.objects.filter(
                organization_id=document.organization_id,
                patient_id=document.patient_id,
                channel=DELIVERY_CHANNEL,
            )
            .filter(verified_version__gte=1)
            .first()
        )
        if contact is None or contact.verified_version != contact.destination_version:
            raise PermanentSendError
        verification_url = str(document.frozen_input.get("verification_url", ""))
        if not verification_url:
            raise PermanentSendError
        body = render_to_string(
            "comms/email_document_delivery_v1.txt",
            {
                "clinic": operation.clinic.name,
                "verification_url": verification_url,
            },
        ).strip()
        return DocumentDeliveryMessage(
            channel=DELIVERY_CHANNEL,
            destination=contact.destination,
            body=body,
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return only explicitly synthetic acceptance, or fail closed."""
        if (
            not isinstance(prepared, DocumentDeliveryMessage)
            or prepared.channel != DELIVERY_CHANNEL
            or not channel_capability(DELIVERY_CHANNEL).synthetic_enabled
        ):
            raise PermanentSendError
        return SendResult(f"synthetic:{DELIVERY_CHANNEL}:{operation_id}")


__all__ = ["DocumentDeliveryAdapter", "DocumentDeliveryMessage"]
