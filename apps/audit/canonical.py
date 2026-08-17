"""Typed canonical audit content and hashing boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address
from typing import Final, TypeGuard
from uuid import UUID

import rfc8785

AUDIT_PAYLOAD_ALLOWED: Final[frozenset[str]] = frozenset(
    {
        "clinic_id",
        "http_method",
        "http_status",
        "object_verb",
        "reason_code",
        "request_id",
    }
)
_STRING_LIMITS: Final[Mapping[str, int]] = {
    "http_method": 16,
    "object_verb": 64,
    "reason_code": 255,
    "request_id": 255,
}
_HASH_DOMAIN: Final = b"clinic-audit-v1\x00"
_EVENT_TYPE_MAX: Final = 128
_COMPONENT_ID_MAX: Final = 255
_AFFECTED_TYPE_MAX: Final = 128
_AFFECTED_ID_MAX: Final = 255
_HTTP_STATUS_MIN: Final = 100
_HTTP_STATUS_MAX: Final = 599
_SURROGATE_CODE_POINT_MIN: Final = 0xD800
_SURROGATE_CODE_POINT_MAX: Final = 0xDFFF
_COMPONENT_IP_TYPES: Final = (IPv4Address, IPv6Address)

type AuditPayloadInputValue = (
    str | int | bool | float | Decimal | None | Sequence[str] | Mapping[str, str]
)
type ValidAuditPayload = Mapping[str, str | int]
type CanonicalValue = (
    bool
    | int
    | str
    | float
    | None
    | Sequence["CanonicalValue"]
    | Mapping[str, "CanonicalValue"]
)


@dataclass(frozen=True, slots=True)
class AuditPayloadKeyRejectedError(Exception):
    """Report one payload key outside the fixed audit vocabulary."""

    key: str

    def __str__(self) -> str:
        """Return a safe key-only rejection message."""
        return f"audit payload key rejected: {self.key}"


AuditPayloadKeyRejected = AuditPayloadKeyRejectedError


@dataclass(frozen=True, slots=True)
class AuditPayloadValueRejectedError(Exception):
    """Report one payload value outside its fixed scalar domain."""

    key: str

    def __str__(self) -> str:
        """Return a safe key-only rejection message."""
        return f"audit payload value rejected: {self.key}"


@dataclass(frozen=True, slots=True)
class AuditEventValueRejectedError(Exception):
    """Report one malformed semantic event field."""

    field: str

    def __str__(self) -> str:
        """Return a safe field-only rejection message."""
        return f"audit event field rejected: {self.field}"


@dataclass(frozen=True, slots=True)
class AuditEventInput:
    """Typed semantic fields supplied by an audit producer."""

    event_type: str
    component_id: str
    component_ip: IPv4Address | IPv6Address | None
    affected_record_type: str | None
    affected_record_id: str | None
    occurred_at_utc: datetime

    def __post_init__(self) -> None:
        """Reject invalid semantic values before canonicalization."""
        event_type = _freeze_text(self.event_type)
        if event_type is None or not _is_bounded_text(event_type, _EVENT_TYPE_MAX):
            raise AuditEventValueRejectedError(field="event_type")
        object.__setattr__(self, "event_type", event_type)
        component_id = _freeze_text(self.component_id)
        if component_id is None or not _is_bounded_text(
            component_id, _COMPONENT_ID_MAX
        ):
            raise AuditEventValueRejectedError(field="component_id")
        object.__setattr__(self, "component_id", component_id)
        if (
            self.component_ip is not None
            and type(self.component_ip) not in _COMPONENT_IP_TYPES
        ):
            raise AuditEventValueRejectedError(field="component_ip")
        if (self.affected_record_type is None) != (self.affected_record_id is None):
            raise AuditEventValueRejectedError(field="affected_record")
        if self.affected_record_type is not None:
            affected_record_type = _freeze_text(self.affected_record_type)
            affected_record_id = _freeze_text(self.affected_record_id)
            if (
                affected_record_type is None
                or affected_record_id is None
                or not _is_bounded_text(
                    affected_record_type,
                    _AFFECTED_TYPE_MAX,
                )
                or not _is_bounded_text(affected_record_id, _AFFECTED_ID_MAX)
            ):
                raise AuditEventValueRejectedError(field="affected_record")
            object.__setattr__(
                self,
                "affected_record_type",
                affected_record_type,
            )
            object.__setattr__(self, "affected_record_id", affected_record_id)
        occurred_at_utc = self.occurred_at_utc
        if type(occurred_at_utc) is not datetime:
            raise AuditEventValueRejectedError(field="occurred_at_utc")
        try:
            utc_offset = occurred_at_utc.utcoffset()
            if utc_offset is None:
                raise AuditEventValueRejectedError(field="occurred_at_utc")
            normalized_timestamp = occurred_at_utc.replace(tzinfo=None) - utc_offset
        except (OverflowError, TypeError, ValueError) as error:
            raise AuditEventValueRejectedError(field="occurred_at_utc") from error
        object.__setattr__(
            self, "occurred_at_utc", normalized_timestamp.replace(tzinfo=UTC)
        )


@dataclass(frozen=True, slots=True)
class AuditTrustedContext:
    """Canonical organization and optional actor read from database GUCs."""

    organization_id: UUID
    actor_user_id: UUID | None


def _freeze_text(value: AuditPayloadInputValue) -> str | None:
    if not isinstance(value, str):
        return None
    return str.__str__(value)


def _is_integer(value: AuditPayloadInputValue) -> TypeGuard[int]:
    return type(value) is int


def _is_bounded_text(value: str, maximum: int) -> bool:
    return (
        len(value) <= maximum
        and bool(value.strip())
        and "\x00" not in value
        and all(
            not _SURROGATE_CODE_POINT_MIN <= ord(character) <= _SURROGATE_CODE_POINT_MAX
            for character in value
        )
    )


def _normalize_payload(
    payload: Mapping[str, AuditPayloadInputValue],
) -> ValidAuditPayload:
    normalized: dict[str, str | int] = {}
    for raw_key, value in payload.items():
        key = _freeze_text(raw_key)
        if key is None:
            raise AuditPayloadKeyRejected(key="<non-string>")
        if key not in AUDIT_PAYLOAD_ALLOWED:
            raise AuditPayloadKeyRejected(key=key)
        if key == "clinic_id":
            normalized_value = _freeze_text(value)
            if normalized_value is None or not _is_canonical_uuid(normalized_value):
                raise AuditPayloadValueRejectedError(key=key)
            normalized[key] = normalized_value
        elif (string_limit := _STRING_LIMITS.get(key)) is not None:
            normalized_value = _freeze_text(value)
            if normalized_value is None or not _is_bounded_text(
                normalized_value,
                string_limit,
            ):
                raise AuditPayloadValueRejectedError(key=key)
            normalized[key] = normalized_value
        elif not _is_integer(value) or not (
            _HTTP_STATUS_MIN <= value <= _HTTP_STATUS_MAX
        ):
            raise AuditPayloadValueRejectedError(key=key)
        else:
            normalized[key] = value
    return normalized


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _content_hash(
    event: AuditEventInput,
    context: AuditTrustedContext,
    payload: ValidAuditPayload,
) -> bytes:
    occurred_at_utc = event.occurred_at_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    actor_id = None if context.actor_user_id is None else str(context.actor_user_id)
    component_ip = None if event.component_ip is None else str(event.component_ip)
    content: dict[str, CanonicalValue] = {
        "v": "clinic-audit-v1",
        "organization_id": str(context.organization_id),
        "event_type": event.event_type,
        "component": {"identifier": event.component_id, "ip": component_ip},
        "actor": {"user_id": actor_id},
        "affected_record_type": event.affected_record_type,
        "affected_record_id": event.affected_record_id,
        "occurred_at_utc": occurred_at_utc,
        "payload": dict(payload),
    }
    return sha256(_HASH_DOMAIN + rfc8785.dumps(content)).digest()
