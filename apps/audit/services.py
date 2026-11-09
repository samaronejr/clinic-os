"""Trusted-context audit append service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import connection, connections

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
from apps.audit.events import (
    PHASE1_AUDIT_EVENTS,
    Phase1AuditAppend,
    Phase1AuditEventDefinition,
    Phase1AuditEventRejectedError,
    build_phase1_audit_event,
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
    from collections.abc import Callable, Mapping

__all__ = (
    "AUDIT_PAYLOAD_ALLOWED",
    "PHASE1_AUDIT_EVENTS",
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
    "Phase1AuditAppend",
    "Phase1AuditEventDefinition",
    "Phase1AuditEventRejectedError",
    "SystemAuditAccessRejectedError",
    "ValidAuditPayload",
    "_content_hash",
    "_normalize_payload",
    "_record_system_event",
    "build_phase1_audit_event",
    "record_event",
    "record_phase1_event",
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


def record_phase1_event(
    event_type: str,
    *,
    clinic_id: UUID,
    affected_record_id: UUID,
) -> int:
    """Append one exact fixed-matrix event through its authorized chain."""
    append = build_phase1_audit_event(
        event_type,
        clinic_id=clinic_id,
        affected_record_id=affected_record_id,
    )
    if append.chain == "system":
        return _record_system_event(append.event, payload=append.payload)
    if event_type.startswith("retention.purge."):
        return _record_retention_event(append, affected_record_id)
    if "agent" in connections and connections["agent"].in_atomic_block:
        from apps.ai.action_audit import record_agent_event  # noqa: PLC0415

        return record_agent_event(append, affected_record_id)
    session_seq = _record_session_event(append, clinic_id, affected_record_id)
    if session_seq is not None:
        return session_seq
    for prefix, record in _PRINCIPAL_CHAIN_RECORDERS:
        if event_type.startswith(prefix) and _current_principal() is not None:
            return record(append, affected_record_id)
    return record_event(append.event, payload=append.payload)


def _session_setting(name: str) -> bool:
    """Return whether one transaction-local session GUC is set and nonblank."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT NULLIF(current_setting(%s,true),'') IS NOT NULL", [name])
        row = cursor.fetchone()
    return row is not None and row[0] is True


def _record_session_event(
    append: Phase1AuditAppend, clinic_id: UUID, record_id: UUID
) -> int | None:
    """Bind directory and delegate events to their authenticated session chain."""
    event_type = append.event.event_type
    if event_type.startswith("identity.scim.") and _session_setting(
        "app.current_scim_token"
    ):
        return _record_scim_event(append, clinic_id)
    if event_type.startswith("identity.delegate.") and _session_setting(
        "app.current_patient_session"
    ):
        return _record_delegate_event(append, record_id)
    return None


def _current_principal() -> str | None:
    """Return the machine principal GUC, or None for a human or patient context."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_principal',true),'')"
        )
        row = cursor.fetchone()
    return None if row is None else row[0]


def _record_retention_event(append: Phase1AuditAppend, record_id: UUID) -> int:
    """Bind the canonical hash to the actual executing retention principal."""
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.retention_audit_scope(%s,%s)",
            [record_id, append.event.event_type],
        )
        scope = cursor.fetchone()
        if (
            scope is None
            or append.payload.get("clinic_id") != str(scope[1])
            or append.event.affected_record_type != scope[3]
        ):
            raise AuditContextRejectedError(setting="retention_audit_scope")
        content_hash = _content_hash(
            append.event,
            AuditTrustedContext(organization_id=scope[0], actor_user_id=scope[2]),
            _normalize_payload(append.payload),
        )
        cursor.execute(
            "SELECT clinic_app.retention_audit(%s,%s,%s,%s)",
            [
                record_id,
                append.event.event_type,
                append.event.occurred_at_utc,
                content_hash,
            ],
        )
        row = cursor.fetchone()
    if row is None:
        raise AuditContextRejectedError(setting="retention_audit")
    return int(row[0])


def _record_scim_event(append: Phase1AuditAppend, clinic_id: UUID) -> int:
    """Hash the authenticated credential principal without installing a staff actor."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.enterprise_scim_audit_scope(%s)", [clinic_id]
        )
        scope = cursor.fetchone()
        if scope is None:
            raise AuditContextRejectedError(setting="enterprise_scim_audit_scope")
        context = AuditTrustedContext(organization_id=scope[1], actor_user_id=scope[0])
        content_hash = _content_hash(
            append.event, context, _normalize_payload(append.payload)
        )
        cursor.execute(
            "SELECT clinic_app.audit_append("
            "%s::text, %s::text, %s::inet, %s::text, %s::text, "
            "%s::timestamptz, %s::jsonb, %s::bytea)",
            [
                append.event.event_type,
                append.event.component_id,
                None,
                append.event.affected_record_type,
                append.event.affected_record_id,
                append.event.occurred_at_utc,
                json.dumps(dict(append.payload)),
                content_hash,
            ],
        )
        result = cursor.fetchone()
    if result is None:
        raise AuditContextRejectedError(setting="audit_append")
    return int(result[0])


def _record_lab_event(append: Phase1AuditAppend, record_id: UUID) -> int:
    """Bind lab audit to the authenticated source principal without a staff GUC."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.orders_audit_scope(%s,%s)",
            [record_id, append.event.event_type],
        )
        scope = cursor.fetchone()
        if scope is None or append.payload.get("clinic_id") != str(scope[2]):
            raise AuditContextRejectedError(setting="orders_audit_scope")
        context = AuditTrustedContext(organization_id=scope[1], actor_user_id=scope[0])
        content_hash = _content_hash(
            append.event, context, _normalize_payload(append.payload)
        )
        cursor.execute(
            "SELECT clinic_app.orders_lab_audit(%s,%s,%s,%s)",
            [
                record_id,
                append.event.event_type,
                append.event.occurred_at_utc,
                content_hash,
            ],
        )
        result = cursor.fetchone()
    if result is None:
        raise AuditContextRejectedError(setting="orders_lab_audit")
    return int(result[0])


def _record_comms_ingest_event(append: Phase1AuditAppend, record_id: UUID) -> int:
    """Bind inbox ingest audit to the authenticated provider principal."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.comms_audit_scope(%s,%s)",
            [record_id, append.event.event_type],
        )
        scope = cursor.fetchone()
        if scope is None or append.payload.get("clinic_id") != str(scope[2]):
            raise AuditContextRejectedError(setting="comms_audit_scope")
        context = AuditTrustedContext(organization_id=scope[1], actor_user_id=scope[0])
        content_hash = _content_hash(
            append.event, context, _normalize_payload(append.payload)
        )
        cursor.execute(
            "SELECT clinic_app.comms_ingest_audit(%s,%s,%s,%s)",
            [
                record_id,
                append.event.event_type,
                append.event.occurred_at_utc,
                content_hash,
            ],
        )
        result = cursor.fetchone()
    if result is None:
        raise AuditContextRejectedError(setting="comms_ingest_audit")
    return int(result[0])


def _record_delegate_event(append: Phase1AuditAppend, record_id: UUID) -> int:
    """Use stored patient authority without ever installing staff tenant GUCs."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.delegate_audit_scope(%s,%s)",
            [record_id, append.event.event_type],
        )
        scope = cursor.fetchone()
        if scope is None or append.payload.get("clinic_id") != str(scope[2]):
            raise AuditContextRejectedError(setting="delegate_audit_scope")
        context = AuditTrustedContext(organization_id=scope[1], actor_user_id=scope[0])
        content_hash = _content_hash(
            append.event, context, _normalize_payload(append.payload)
        )
        cursor.execute(
            "SELECT clinic_app.delegate_audit(%s,%s,%s,%s)",
            [
                record_id,
                append.event.event_type,
                append.event.occurred_at_utc,
                content_hash,
            ],
        )
        result = cursor.fetchone()
    if result is None:
        raise AuditContextRejectedError(setting="delegate_audit")
    return int(result[0])


def verify_chain(
    organization_id: UUID | None = None,
) -> AuditChainVerificationResult:
    """Verify one tenant chain or the owner-only system chain."""
    return verify_chain_on_connection(connection, organization_id)


# Event families whose rows bind the authenticated provider principal.
_PRINCIPAL_CHAIN_RECORDERS: tuple[
    tuple[str, Callable[[Phase1AuditAppend, UUID], int]], ...
] = (
    ("orders.", _record_lab_event),
    ("comms.inbound.", _record_comms_ingest_event),
)
