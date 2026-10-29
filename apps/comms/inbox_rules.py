"""Pure inbox policy: purposes, the WhatsApp service window and send decisions.

Nothing here touches the database, the clock or a provider. Callers pass the
instants they already hold, so every rule is deterministic and testable without
a tenant context. The decisions mirror Meta's published rules (checked
2026-10-29 against developers.facebook.com/docs/whatsapp/pricing): non-template
messages are valid only inside the 24 hour customer service window that each
inbound message opens or resets; outside it only an approved template may be
sent. A conversation's purpose never changes, and marketing never shares a
thread with transactional or care content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

SERVICE_WINDOW: Final = timedelta(hours=24)
CHANNELS: Final = ("whatsapp", "email", "sms", "portal")
EXTERNAL_CHANNELS: Final = ("whatsapp", "email", "sms")
MARKETING_CHANNELS: Final = ("whatsapp", "email", "sms")
WINDOWED_CHANNELS: Final = ("whatsapp",)
MAX_BODY_CHARS: Final = 4096


class Purpose(StrEnum):
    """Why a conversation exists; fixed when the thread is created."""

    TRANSACTIONAL = "transactional"
    CARE = "care"
    MARKETING = "marketing"


class TemplateCategory(StrEnum):
    """Meta template categories; each is metered and approved separately."""

    UTILITY = "utility"
    MARKETING = "marketing"
    AUTHENTICATION = "authentication"


class ApprovalStatus(StrEnum):
    """Provider approval lifecycle of one immutable template version."""

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAUSED = "paused"
    DISABLED = "disabled"


class DeliveryState(StrEnum):
    """What the inbox shows for an outbound message."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


# Provider-reported progress is monotonic; failure is terminal and absorbing
# unless the provider later proves delivery, so ranks are compared, not replaced.
DELIVERY_RANK: Final = {
    DeliveryState.QUEUED: 0,
    DeliveryState.SENT: 1,
    DeliveryState.DELIVERED: 2,
    DeliveryState.READ: 3,
}
_PROVIDER_STATES: Final = {
    "sent": DeliveryState.SENT,
    "delivered": DeliveryState.DELIVERED,
    "read": DeliveryState.READ,
    "failed": DeliveryState.FAILED,
}
_OPERATION_STATES: Final = {
    "pending": DeliveryState.QUEUED,
    "in_progress": DeliveryState.QUEUED,
    "succeeded": DeliveryState.SENT,
    "delivered": DeliveryState.DELIVERED,
    "failed": DeliveryState.FAILED,
    "cancelled": DeliveryState.FAILED,
}
_TEMPLATE_PURPOSES: Final = {
    TemplateCategory.UTILITY: frozenset({Purpose.TRANSACTIONAL, Purpose.CARE}),
    TemplateCategory.AUTHENTICATION: frozenset({Purpose.TRANSACTIONAL}),
    TemplateCategory.MARKETING: frozenset({Purpose.MARKETING}),
}


def window_open(*, last_inbound_at: datetime | None, now: datetime) -> bool:
    """Return whether the customer service window is open at ``now``.

    The window is exclusive at its end: exactly 24 hours after the last inbound
    message it is already closed, so a boundary tick can never send free text.
    """
    if last_inbound_at is None or now < last_inbound_at:
        return False
    return now - last_inbound_at < SERVICE_WINDOW


def window_closes_at(*, last_inbound_at: datetime | None) -> datetime | None:
    """Return the instant the current window closes, or None when never opened."""
    return None if last_inbound_at is None else last_inbound_at + SERVICE_WINDOW


def template_fits_purpose(*, category: str, purpose: str) -> bool:
    """Keep marketing out of care threads and care content out of marketing."""
    try:
        allowed = _TEMPLATE_PURPOSES[TemplateCategory(category)]
        return Purpose(purpose) in allowed
    except ValueError:
        return False


def channel_allows_purpose(*, channel: str, purpose: str) -> bool:
    """Marketing is never delivered in-app; every other pairing is allowed."""
    if channel not in CHANNELS or purpose not in Purpose:
        return False
    return purpose != Purpose.MARKETING or channel in MARKETING_CHANNELS


@dataclass(frozen=True, slots=True)
class SendDecision:
    """The outcome of a send check; ``reason`` is a fixed, content-free code."""

    allowed: bool
    reason: str
    # How the provider will categorize the message for metering.
    meter_category: str = ""


@dataclass(frozen=True, slots=True)
class SendRequest:
    """Facts about one proposed outbound message; ``template_*`` is None for text."""

    channel: str
    purpose: str
    conversation_state: str
    now: datetime
    last_inbound_at: datetime | None
    template_category: str | None
    template_status: str | None
    template_channel: str | None
    linked_to_patient: bool


def _refuse(reason: str) -> SendDecision:
    return SendDecision(allowed=False, reason=reason)


def _free_text(request: SendRequest) -> SendDecision:
    if request.purpose == Purpose.MARKETING:
        return _refuse("marketing_requires_template")
    if request.channel in WINDOWED_CHANNELS and not window_open(
        last_inbound_at=request.last_inbound_at, now=request.now
    ):
        return _refuse("window_expired_template_required")
    meter = "service" if request.channel == "whatsapp" else ""
    return SendDecision(allowed=True, reason="ok", meter_category=meter)


def _template(request: SendRequest, category: str) -> SendDecision:
    if request.template_channel != request.channel:
        return _refuse("template_channel_mismatch")
    if request.template_status != ApprovalStatus.APPROVED:
        return _refuse("template_not_approved")
    if not template_fits_purpose(category=category, purpose=request.purpose):
        return _refuse("template_purpose_mismatch")
    meter = category if request.channel == "whatsapp" else ""
    return SendDecision(allowed=True, reason="ok", meter_category=meter)


def decide_send(request: SendRequest) -> SendDecision:
    """Decide whether one outbound message may be queued right now.

    Free text on a windowed channel needs an open window; a template needs
    provider approval on the same channel and a category that fits the thread's
    purpose. Care content additionally needs a verified patient link, so an
    unverified number never receives it.
    """
    if request.channel not in CHANNELS or request.purpose not in Purpose:
        return _refuse("invalid_scope")
    if request.conversation_state == "closed":
        return _refuse("conversation_closed")
    if not channel_allows_purpose(channel=request.channel, purpose=request.purpose):
        return _refuse("channel_purpose_mismatch")
    if request.purpose == Purpose.CARE and not request.linked_to_patient:
        return _refuse("patient_not_verified")
    if request.template_category is None:
        return _free_text(request)
    return _template(request, request.template_category)


def effective_delivery_state(*, operation_status: str, provider_state: str) -> str:
    """Combine the outbox lifecycle with the provider-reported status.

    The outbox says whether the provider accepted the message; the provider's
    status webhook can only advance it. A failure from either side wins over
    any non-terminal progress, and a stale lower rank never rewinds the state.
    """
    base = _OPERATION_STATES.get(operation_status, DeliveryState.QUEUED)
    reported = _PROVIDER_STATES.get(provider_state)
    if DeliveryState.FAILED in (base, reported):
        return DeliveryState.FAILED
    if reported is not None and DELIVERY_RANK[reported] > DELIVERY_RANK[base]:
        return reported
    return base


def advance_provider_state(*, current: str, incoming: str) -> str:
    """Return the stored provider state after a status event, never a lower one."""
    new = _PROVIDER_STATES.get(incoming)
    if new is None:
        return current
    old = _PROVIDER_STATES.get(current)
    if old is None or old == DeliveryState.FAILED:
        # Delivery evidence may replace an earlier failure report.
        return new
    if new == DeliveryState.FAILED:
        # A failure never overwrites proof of delivery.
        return old if old in (DeliveryState.DELIVERED, DeliveryState.READ) else new
    return new if DELIVERY_RANK[new] > DELIVERY_RANK[old] else old
