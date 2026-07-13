"""Trusted-context audit append service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import connection

from apps.audit.canonical import (
    AUDIT_PAYLOAD_ALLOWED,
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditPayloadInputValue,
    AuditPayloadKeyRejected,
    AuditPayloadValueRejectedError,
    AuditTrustedContext,
    CanonicalValue,
    ValidAuditPayload,
    _content_hash,
    _normalize_payload,
)
from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.verification import (
    AuditChainFailureKind,
    AuditChainVerificationError,
    AuditChainVerificationResult,
    AuditVerificationAccessRejectedError,
    verify_chain_on_connection,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = (
    "AUDIT_PAYLOAD_ALLOWED",
    "AuditChainFailureKind",
    "AuditChainVerificationError",
    "AuditChainVerificationResult",
    "AuditContextRejectedError",
    "AuditEventInput",
    "AuditEventValueRejectedError",
    "AuditPayloadInputValue",
    "AuditPayloadKeyRejected",
    "AuditPayloadValueRejectedError",
    "AuditTrustedContext",
    "AuditVerificationAccessRejectedError",
    "CanonicalValue",
    "SystemAuditAccessRejectedError",
    "ValidAuditPayload",
    "_content_hash",
    "_normalize_payload",
    "_record_system_event",
    "record_event",
    "verify_chain",
)


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


def verify_chain(
    organization_id: UUID | None = None,
) -> AuditChainVerificationResult:
    """Verify one tenant chain or the owner-only system chain."""
    return verify_chain_on_connection(connection, organization_id)
