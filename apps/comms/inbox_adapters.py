"""Outbound inbox adapters on the shared outbox, plus the send-time recheck.

Preparation runs inside the worker's tenant transaction and rebuilds the request
from stored rows; the network call happens afterwards with no open transaction.
No live transport exists: a real send is blocked on EG-1 (provider selection and
owner approval), so ``send`` answers only an explicitly synthetic acceptance
reference for channels enabled as synthetic, or fails closed. The recheck runs
the same window, template and consent rules as the command, so a message queued
before the window closed, a template was revoked or a thread was closed is
cancelled rather than sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.utils import timezone

from apps.comms.adapters import PermanentSendError, SendResult, TransientSendError
from apps.comms.capabilities import channel_capability
from apps.comms.inbox_rules import SendRequest, decide_send
from apps.comms.inbox_services import (
    PROVIDER_BY_CHANNEL,
    SUBJECT_TYPE,
    conversation_lock_key,
    marketing_opted_in,
)
from apps.comms.models import IntegrationOperation, Message
from apps.comms.whatsapp_payloads import template_payload, text_payload

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.comms.adapters import OperationScope


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Ephemeral request, never stored or logged; the body lives only in memory."""

    channel: str
    destination: str
    body: str
    payload: dict[str, object]


def _message_for(operation: IntegrationOperation) -> Message:
    return Message.objects.select_related("conversation", "template").get(
        pk=operation.subject_id, direction="out"
    )


class InboxAdapter:
    """Shared mechanics; each channel keeps its own capability gate and provider."""

    channel: ClassVar[str]
    provider: str

    def prepare(self, operation: IntegrationOperation) -> OutboundMessage:
        """Rebuild the request from stored rows under current tenant authority."""
        if operation.channel != self.channel or operation.subject_type != SUBJECT_TYPE:
            raise PermanentSendError
        message = _message_for(operation)
        conversation = message.conversation
        if conversation.channel != self.channel:
            raise PermanentSendError
        destination = conversation.contact_address
        template = message.template
        payload: dict[str, object] = {}
        if self.channel == "whatsapp":
            payload = (
                template_payload(
                    to=destination,
                    name=template.name,
                    language=template.language,
                    parameters=tuple(json.loads(message.template_parameters or "[]")),
                )
                if template is not None
                else text_payload(to=destination, body=message.body)
            )
        return OutboundMessage(self.channel, destination, message.body, payload)

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return only a synthetic acceptance reference, or fail closed."""
        if (
            not isinstance(prepared, OutboundMessage)
            or prepared.channel != self.channel
        ):
            raise PermanentSendError
        if self.channel == "portal":
            return SendResult(f"portal:{operation_id}")
        if not channel_capability(self.channel).synthetic_enabled:
            # Real delivery has no transport until EG-1; never pretend otherwise.
            raise PermanentSendError
        if self.channel in getattr(settings, "COMMS_SYNTHETIC_FAILURE_CHANNELS", ()):
            raise TransientSendError
        return SendResult(f"synthetic:{self.channel}:{operation_id}")


class WhatsAppInboxAdapter(InboxAdapter):
    """Cloud API shaped requests; synthetic acceptance only."""

    channel = "whatsapp"
    provider = PROVIDER_BY_CHANNEL["whatsapp"]


class EmailInboxAdapter(InboxAdapter):
    """Email replies, separately gated from both phone channels."""

    channel = "email"
    provider = PROVIDER_BY_CHANNEL["email"]


class SMSInboxAdapter(InboxAdapter):
    """SMS replies, separately gated from email and WhatsApp."""

    channel = "sms"
    provider = PROVIDER_BY_CHANNEL["sms"]


class PortalInboxAdapter(InboxAdapter):
    """In-app delivery: nothing leaves the system, so no capability gate applies."""

    channel = "portal"
    provider = PROVIDER_BY_CHANNEL["portal"]


_SEND_CHECKS: list[Callable[[Message], bool]] = []


def register_message_send_check(check: Callable[[Message], bool]) -> None:
    """Let the domain that queued an outbound message veto it at send time.

    Each check runs inside the send's tenant transaction, under the sender's
    authority, after the channel rules; any ``False`` cancels the operation.
    """
    if check not in _SEND_CHECKS:
        _SEND_CHECKS.append(check)


def message_send_eligible(scope: OperationScope) -> bool:
    """Re-decide the send under current facts; False cancels without a side effect."""
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    message = (
        Message.objects.select_related("conversation", "template")
        .filter(pk=operation.subject_id, direction="out")
        .first()
    )
    if message is None or message.conversation.clinic_id != scope.clinic_id:
        return False
    conversation, template = message.conversation, message.template
    decision = decide_send(
        SendRequest(
            channel=conversation.channel,
            purpose=conversation.purpose,
            conversation_state=conversation.state,
            now=timezone.now(),
            last_inbound_at=conversation.last_inbound_at,
            template_category=template.category if template else None,
            template_status=template.approval_status if template else None,
            template_channel=template.channel if template else None,
            linked_to_patient=conversation.enrollment_id is not None,
        )
    )
    return (
        decision.allowed
        and (
            conversation.purpose != "marketing"
            or marketing_opted_in(
                enrollment_id=conversation.enrollment_id, channel=conversation.channel
            )
        )
        and all(check(message) for check in _SEND_CHECKS)
    )


def message_lock_key(scope: OperationScope, subject_id: UUID) -> str | None:
    """Serialize sends with thread mutations such as close and patient link."""
    conversation_id = (
        Message.objects.filter(pk=subject_id, conversation__clinic_id=scope.clinic_id)
        .values_list("conversation_id", flat=True)
        .first()
    )
    return None if conversation_id is None else conversation_lock_key(conversation_id)
