"""Use the canonical ledger with the actual machine principal, never a human GUC."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connections

from apps.audit.canonical import AuditTrustedContext, _content_hash, _normalize_payload
from apps.audit.services import AuditContextRejectedError

if TYPE_CHECKING:
    from uuid import UUID

    from apps.audit.events import Phase1AuditAppend


def record_agent_event(append: Phase1AuditAppend, record_id: UUID) -> int:
    """Resolve the scope from retained state before hashing and appending."""
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.agent_audit_scope(%s,%s)",
            [
                record_id,
                append.event.event_type,
            ],
        )
        scope = cursor.fetchone()
        if (
            scope is None
            or append.payload.get("clinic_id") != str(scope[1])
            or append.event.affected_record_type != scope[3]
        ):
            raise AuditContextRejectedError(setting="agent_audit_scope")
        context = AuditTrustedContext(organization_id=scope[0], actor_user_id=scope[2])
        content_hash = _content_hash(
            append.event, context, _normalize_payload(append.payload)
        )
        cursor.execute(
            "SELECT clinic_app.agent_audit(%s,%s,%s,%s)",
            [
                record_id,
                append.event.event_type,
                append.event.occurred_at_utc,
                content_hash,
            ],
        )
        row = cursor.fetchone()
    if row is None:
        raise AuditContextRejectedError(setting="agent_audit")
    return int(row[0])
