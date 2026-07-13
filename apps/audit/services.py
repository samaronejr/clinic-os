"""Trusted-context audit append service."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Final, TypeGuard
from uuid import UUID

import rfc8785
from django.db import connection

from apps.audit.models import SYSTEM_ORG_ID

if TYPE_CHECKING:
    from ipaddress import IPv4Address, IPv6Address

AUDIT_PAYLOAD_ALLOWED: Final[frozenset[str]] = frozenset(
    {"http_method", "http_status", "object_verb", "reason_code", "request_id"}
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
class AuditPayloadKeyRejected(Exception):  # noqa: N818
    """Report one payload key outside the fixed audit vocabulary."""

    key: str

    def __str__(self) -> str:
        """Return a safe key-only rejection message."""
        return f"audit payload key rejected: {self.key}"


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
class AuditContextRejectedError(Exception):
    """Report malformed trusted database context."""

    setting: str

    def __str__(self) -> str:
        """Return a safe setting-only rejection message."""
        return f"audit context rejected: {self.setting}"


@dataclass(frozen=True, slots=True)
class SystemAuditAccessRejectedError(Exception):
    """Report use of the internal system path by a non-owner connection."""

    database_role: str

    def __str__(self) -> str:
        """Return a role-only rejection message."""
        return f"system audit append requires clinic_owner, got {self.database_role}"


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
        if not 1 <= len(self.event_type.strip()) <= _EVENT_TYPE_MAX:
            raise AuditEventValueRejectedError(field="event_type")
        if not 1 <= len(self.component_id.strip()) <= _COMPONENT_ID_MAX:
            raise AuditEventValueRejectedError(field="component_id")
        if (self.affected_record_type is None) != (self.affected_record_id is None):
            raise AuditEventValueRejectedError(field="affected_record")
        if (
            self.affected_record_type is not None
            and self.affected_record_id is not None
            and not (
                1 <= len(self.affected_record_type.strip()) <= _AFFECTED_TYPE_MAX
                and 1 <= len(self.affected_record_id.strip()) <= _AFFECTED_ID_MAX
            )
        ):
            raise AuditEventValueRejectedError(field="affected_record")
        if self.occurred_at_utc.tzinfo is None:
            raise AuditEventValueRejectedError(field="occurred_at_utc")


@dataclass(frozen=True, slots=True)
class AuditTrustedContext:
    """Canonical organization and optional actor read from database GUCs."""

    organization_id: UUID
    actor_user_id: UUID | None


def _is_text(value: AuditPayloadInputValue) -> TypeGuard[str]:
    return isinstance(value, str)


def _is_integer(value: AuditPayloadInputValue) -> TypeGuard[int]:
    return type(value) is int


def _normalize_payload(
    payload: Mapping[str, AuditPayloadInputValue],
) -> ValidAuditPayload:
    normalized: dict[str, str | int] = {}
    for key, value in payload.items():
        if key not in AUDIT_PAYLOAD_ALLOWED:
            raise AuditPayloadKeyRejected(key=key)
        string_limit = _STRING_LIMITS.get(key)
        if string_limit is not None:
            if not _is_text(value) or not 1 <= len(value) <= string_limit:
                raise AuditPayloadValueRejectedError(key=key)
            normalized[key] = value
        elif not _is_integer(value) or not (
            _HTTP_STATUS_MIN <= value <= _HTTP_STATUS_MAX
        ):
            raise AuditPayloadValueRejectedError(key=key)
        else:
            normalized[key] = value
    return normalized


def _content_hash(
    event: AuditEventInput,
    context: AuditTrustedContext,
    payload: ValidAuditPayload,
) -> bytes:
    occurred_at_utc = event.occurred_at_utc.astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
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


def _parse_required_context(raw_value: str | None, setting: str) -> UUID:
    if raw_value is None or raw_value == "":
        raise AuditContextRejectedError(setting=setting)
    try:
        return UUID(raw_value)
    except ValueError as error:
        raise AuditContextRejectedError(setting=setting) from error


def _parse_optional_actor(raw_value: str | None) -> UUID | None:
    if raw_value is None or raw_value == "":
        return None
    try:
        return UUID(raw_value)
    except ValueError as error:
        raise AuditContextRejectedError(setting="app.current_user_id") from error


def record_event(
    event: AuditEventInput,
    *,
    payload: Mapping[str, AuditPayloadInputValue],
) -> int:
    """Append one tenant event using organization and actor GUCs."""
    normalized_payload = _normalize_payload(payload)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_tenant', true), ''), "
            "NULLIF(current_setting('app.current_user_id', true), '')"
        )
        row = cursor.fetchone()
        if row is None:
            raise AuditContextRejectedError(setting="app.current_tenant")
        context = AuditTrustedContext(
            organization_id=_parse_required_context(row[0], "app.current_tenant"),
            actor_user_id=_parse_required_context(row[1], "app.current_user_id"),
        )
        content_hash = _content_hash(event, context, normalized_payload)
        cursor.execute(
            "SELECT clinic_app.audit_append("
            "%s::text, %s::text, %s::inet, %s::text, %s::text, "
            "%s::timestamptz, %s::jsonb, %s::bytea)",
            [
                event.event_type,
                event.component_id,
                None if event.component_ip is None else str(event.component_ip),
                event.affected_record_type,
                event.affected_record_id,
                event.occurred_at_utc,
                json.dumps(dict(normalized_payload)),
                content_hash,
            ],
        )
        appended = cursor.fetchone()
    if appended is None:
        raise AuditContextRejectedError(setting="audit_append")
    return int(appended[0])


def _record_system_event(
    event: AuditEventInput,
    *,
    payload: Mapping[str, AuditPayloadInputValue],
) -> int:
    """Append through the owner-only system chain function."""
    normalized_payload = _normalize_payload(payload)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, "
            "NULLIF(current_setting('app.current_user_id', true), '')"
        )
        row = cursor.fetchone()
        if row is None or row[0] != "clinic_owner":
            role = "unknown" if row is None else str(row[0])
            raise SystemAuditAccessRejectedError(database_role=role)
        context = AuditTrustedContext(
            organization_id=SYSTEM_ORG_ID,
            actor_user_id=_parse_optional_actor(row[1]),
        )
        content_hash = _content_hash(event, context, normalized_payload)
        cursor.execute(
            "SELECT clinic_app.audit_append_system("
            "%s::text, %s::text, %s::inet, %s::text, %s::text, "
            "%s::timestamptz, %s::jsonb, %s::bytea)",
            [
                event.event_type,
                event.component_id,
                None if event.component_ip is None else str(event.component_ip),
                event.affected_record_type,
                event.affected_record_id,
                event.occurred_at_utc,
                json.dumps(dict(normalized_payload)),
                content_hash,
            ],
        )
        appended = cursor.fetchone()
    if appended is None:
        raise AuditContextRejectedError(setting="audit_append_system")
    return int(appended[0])
