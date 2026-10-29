"""Provider webhook authentication and parsing; pure, no database, no tenant.

Authentication is raw-bytes HMAC-SHA256 over the exact request body (D-19).
Parsing happens only after a signature has verified, and the parsed payload is
never a source of tenant authority: ``account_ref`` is a routing claim the
caller compares with the stored account, nothing more.

WhatsApp Cloud API shapes follow Meta's webhook reference (entry/changes/value,
``messages``, ``statuses`` with a ``pricing`` object, and
``message_template_status_update``), re-read 2026-10-29. Email and SMS have no
selected provider (EG-1); they use one documented signed envelope so the inbox
contract is testable, and a real provider maps onto it in its own adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Final, Literal

MAX_BODY_BYTES: Final = 1024 * 1024
MAX_EVENTS: Final = 100
MAX_TEXT_CHARS: Final = 4096
MAX_ADDRESS_CHARS: Final = 320
MIN_PHONE_DIGITS: Final = 8
MAX_PHONE_DIGITS: Final = 15
MAX_UNIX_SECONDS: Final = 4_102_444_800
MAX_ID_CHARS: Final = 255
SIGNATURE_PREFIX: Final = "sha256="
_DIGITS: Final = re.compile(r"\D+")
_EMAIL: Final = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
PROVIDER_STATUSES: Final = frozenset({"sent", "delivered", "read", "failed"})
TEMPLATE_EVENTS: Final = {
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "PENDING": "pending",
    "PAUSED": "paused",
    "DISABLED": "disabled",
}


class WebhookRejectedError(Exception):
    """One non-identifying refusal for forged, malformed or oversize webhooks."""

    def __init__(self) -> None:
        """Expose a stable message that reflects no payload or account data."""
        super().__init__("webhook rejected")


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One customer message; ``address`` is already normalized for its channel."""

    account_ref: str
    event_id: str
    address: str
    profile_name: str
    text: str
    supported: bool
    sent_at: int


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """One provider delivery status; pricing is the provider's own statement."""

    account_ref: str
    event_id: str
    provider_message_id: str
    status: Literal["sent", "delivered", "read", "failed"]
    sent_at: int
    pricing_category: str = ""
    billable: bool | None = None
    pricing_type: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class TemplateStatus:
    """One provider template approval change, matched by name and language."""

    account_ref: str
    event_id: str
    template_name: str
    language: str
    status: str


type InboundEvent = InboundMessage | StatusUpdate | TemplateStatus


def sign(*, secret: bytes, body: bytes) -> str:
    """Return the ``sha256=<hex>`` header value a provider would send."""
    return SIGNATURE_PREFIX + hmac.digest(secret, body, hashlib.sha256).hex()


def verify_signature(*, secret: bytes, body: bytes, header: str) -> None:
    """Raise unless ``header`` is the HMAC of exactly these raw bytes."""
    if (
        not secret
        or len(body) > MAX_BODY_BYTES
        or not header.startswith(SIGNATURE_PREFIX)
        or not _HEX64.fullmatch(header[len(SIGNATURE_PREFIX) :])
        or not hmac.compare_digest(header, sign(secret=secret, body=body))
    ):
        raise WebhookRejectedError


def normalize_address(channel: str, raw: object) -> str:
    """Return the canonical address for the channel, or raise when it is invalid."""
    if type(raw) is not str or not raw.strip() or len(raw) > MAX_ADDRESS_CHARS:
        raise WebhookRejectedError
    if channel == "email":
        value = raw.strip().lower()
        if _EMAIL.fullmatch(value) is None:
            raise WebhookRejectedError
        return value
    if channel in {"whatsapp", "sms"}:
        digits = _DIGITS.sub("", raw)
        if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
            raise WebhookRejectedError
        return "+" + digits
    raise WebhookRejectedError


def address_fingerprint(*, key: bytes, channel: str, address: str) -> str:
    """Return a keyed, channel-separated hash for equality lookup without plaintext."""
    return hmac.digest(key, f"{channel}\x00{address}".encode(), hashlib.sha256).hex()


def _object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise WebhookRejectedError
    return value


def _list(value: object) -> list[Any]:
    if type(value) is not list or len(value) > MAX_EVENTS:
        raise WebhookRejectedError
    return value


def _text(value: object, *, limit: int = MAX_ID_CHARS, blank: bool = False) -> str:
    if type(value) is not str or len(value) > limit or (not blank and not value):
        raise WebhookRejectedError
    return value


def _seconds(value: object) -> int:
    try:
        number = int(str(value))
    except ValueError as error:
        raise WebhookRejectedError from error
    if not 0 <= number < MAX_UNIX_SECONDS:
        raise WebhookRejectedError
    return number


def _json(body: bytes) -> dict[str, Any]:
    try:
        return _object(json.loads(body))
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise WebhookRejectedError from error


def parse_whatsapp(body: bytes) -> tuple[InboundEvent, ...]:
    """Parse a verified WhatsApp Cloud API webhook into normalized events."""
    root = _json(body)
    if root.get("object") != "whatsapp_business_account":
        raise WebhookRejectedError
    events: list[InboundEvent] = []
    for entry in _list(root.get("entry", [])):
        for change in _list(_object(entry).get("changes", [])):
            change = _object(change)  # noqa: PLW2901
            value = _object(change.get("value", {}))
            if change.get("field") == "message_template_status_update":
                events.append(_whatsapp_template(value, entry))
                continue
            if change.get("field") != "messages":
                continue
            ref = _text(_object(value.get("metadata", {})).get("phone_number_id"))
            names = {
                _text(_object(c).get("wa_id")): _object(c).get("profile", {})
                for c in _list(value.get("contacts", []))
            }
            events.extend(
                _whatsapp_message(ref, m, names)
                for m in _list(value.get("messages", []))
            )
            events.extend(
                _whatsapp_status(ref, s) for s in _list(value.get("statuses", []))
            )
    if len(events) > MAX_EVENTS:
        raise WebhookRejectedError
    return tuple(events)


def _whatsapp_message(ref: str, raw: object, names: dict[str, Any]) -> InboundMessage:
    message = _object(raw)
    sender = _text(message.get("from"))
    kind = message.get("type")
    supported = kind == "text"
    text = (
        _text(_object(message.get("text", {})).get("body"), limit=MAX_TEXT_CHARS)
        if supported
        else ""
    )
    profile = names.get(sender, {})
    name = profile.get("name", "") if type(profile) is dict else ""
    return InboundMessage(
        account_ref=ref,
        event_id=_text(message.get("id")),
        address=normalize_address("whatsapp", sender),
        profile_name=name[:120] if type(name) is str else "",
        text=text,
        supported=supported,
        sent_at=_seconds(message.get("timestamp")),
    )


def _whatsapp_status(ref: str, raw: object) -> StatusUpdate:
    status = _object(raw)
    state = status.get("status")
    if state not in PROVIDER_STATUSES:
        raise WebhookRejectedError
    pricing = _object(status.get("pricing", {}))
    billable = pricing.get("billable")
    errors = status.get("errors", [])
    code = ""
    if type(errors) is list and errors and type(errors[0]) is dict:
        code = str(errors[0].get("code", ""))[:16]
    message_id = _text(status.get("id"))
    stamp = _seconds(status.get("timestamp"))
    return StatusUpdate(
        account_ref=ref,
        event_id=f"{message_id}:{state}:{stamp}",
        provider_message_id=message_id,
        status=state,
        sent_at=stamp,
        pricing_category=_text(pricing.get("category", ""), limit=32, blank=True),
        billable=billable if type(billable) is bool else None,
        pricing_type=_text(pricing.get("type", ""), limit=32, blank=True),
        error_code=code,
    )


def _whatsapp_template(value: dict[str, Any], entry: object) -> TemplateStatus:
    event = _text(value.get("event"), limit=32)
    status = TEMPLATE_EVENTS.get(event)
    if status is None:
        raise WebhookRejectedError
    name = _text(value.get("message_template_name"), limit=512)
    language = _text(value.get("message_template_language"), limit=16)
    template_id = str(value.get("message_template_id", name))[:64]
    return TemplateStatus(
        account_ref=_text(_object(entry).get("id")),
        event_id=f"template:{template_id}:{event}",
        template_name=name,
        language=language,
        status=status,
    )


def parse_signed_envelope(channel: str, body: bytes) -> tuple[InboundEvent, ...]:
    """Parse the documented synthetic email/SMS envelope into normalized events.

    ``{"account": ..., "events": [{"kind": "message"|"status", ...}]}``. Real
    providers are mapped onto this shape by their own adapter once selected.
    """
    root = _json(body)
    ref = _text(root.get("account"))
    events: list[InboundEvent] = []
    for raw in _list(root.get("events", [])):
        item = _object(raw)
        event_id = _text(item.get("event_id"))
        if item.get("kind") == "message":
            events.append(
                InboundMessage(
                    account_ref=ref,
                    event_id=event_id,
                    address=normalize_address(channel, item.get("from")),
                    profile_name="",
                    text=_text(item.get("text"), limit=MAX_TEXT_CHARS),
                    supported=True,
                    sent_at=_seconds(item.get("sent_at")),
                )
            )
        elif item.get("kind") == "status" and item.get("status") in PROVIDER_STATUSES:
            events.append(
                StatusUpdate(
                    account_ref=ref,
                    event_id=event_id,
                    provider_message_id=_text(item.get("provider_message_id")),
                    status=item["status"],
                    sent_at=_seconds(item.get("sent_at")),
                )
            )
        else:
            raise WebhookRejectedError
    return tuple(events)
