"""Typed audit-chain verification over authorized database surfaces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from ipaddress import ip_address
from typing import TYPE_CHECKING, Final, Literal, NamedTuple
from uuid import UUID

from apps.audit.canonical import (
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditPayloadKeyRejected,
    AuditPayloadValueRejectedError,
    AuditTrustedContext,
    _content_hash,
    _normalize_payload,
)
from apps.audit.models import SYSTEM_ORG_ID

_ZERO_HASH: Final = b"\x00" * 32

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.backends.base.base import BaseDatabaseWrapper


class _AuditStoredRow(NamedTuple):
    seq: int
    organization_id: UUID
    actor_user_id: UUID | None
    event_type: str
    component_id: str
    component_ip: str | None
    affected_record_type: str | None
    affected_record_id: str | None
    occurred_at_utc: datetime
    payload: str
    prev_hash: bytes
    curr_hash: bytes


class AuditChainFailureKind(StrEnum):
    """Stable failure categories ordered by the verification contract."""

    GENESIS = "GENESIS"
    LINKAGE = "LINKAGE"
    CONTENT = "CONTENT"


@dataclass(frozen=True, slots=True)
class AuditChainVerificationResult:
    """Explicit successful verification, including the empty-chain case."""

    organization_id: UUID
    row_count: int
    last_seq: int | None
    valid: Literal[True] = field(default=True, init=False)


class AuditChainVerificationError(Exception):
    """Payload-free evidence identifying one invalid chain row."""

    __slots__ = ("kind", "organization_id", "seq")

    organization_id: UUID
    seq: int
    kind: AuditChainFailureKind

    def __init__(
        self,
        organization_id: UUID,
        seq: int,
        kind: AuditChainFailureKind,
    ) -> None:
        """Initialize payload-free failure evidence."""
        self.organization_id = organization_id
        self.seq = seq
        self.kind = kind
        super().__init__()

    def __str__(self) -> str:
        """Return identifiers and failure kind without semantic payload."""
        return (
            f"audit chain {self.kind.value} failure "
            f"organization={self.organization_id} seq={self.seq}"
        )


class AuditVerificationAccessRejectedError(Exception):
    """Report an unauthorized chain-verification surface."""

    __slots__ = ("database_role", "organization_id")

    organization_id: UUID
    database_role: str

    def __init__(self, organization_id: UUID, database_role: str) -> None:
        """Initialize safe access-denial identifiers."""
        self.organization_id = organization_id
        self.database_role = database_role
        super().__init__()

    def __str__(self) -> str:
        """Return role and organization identifiers without ledger content."""
        return (
            "audit verification access rejected "
            f"organization={self.organization_id} role={self.database_role}"
        )


def verify_chain_on_connection(
    database_connection: BaseDatabaseWrapper,
    organization_id: UUID | None = None,
) -> AuditChainVerificationResult:
    """Verify one authorized organization chain in ascending sequence order."""
    requested_organization_id, rows = _authorized_rows(
        database_connection,
        organization_id,
    )
    previous_curr_hash: bytes | None = None
    last_seq: int | None = None
    for row in rows:
        try:
            prev_hash = bytes(row.prev_hash)
        except (TypeError, ValueError):
            failure_kind = (
                AuditChainFailureKind.GENESIS
                if previous_curr_hash is None
                else AuditChainFailureKind.LINKAGE
            )
            raise AuditChainVerificationError(
                organization_id=requested_organization_id,
                seq=row.seq,
                kind=failure_kind,
            ) from None
        if previous_curr_hash is None:
            if prev_hash != _ZERO_HASH:
                raise AuditChainVerificationError(
                    organization_id=requested_organization_id,
                    seq=row.seq,
                    kind=AuditChainFailureKind.GENESIS,
                )
        elif prev_hash != previous_curr_hash:
            raise AuditChainVerificationError(
                organization_id=requested_organization_id,
                seq=row.seq,
                kind=AuditChainFailureKind.LINKAGE,
            )
        expected_curr_hash, curr_hash = _content_hashes(
            row,
            requested_organization_id,
            prev_hash,
        )
        if len(curr_hash) != len(_ZERO_HASH) or curr_hash != expected_curr_hash:
            raise AuditChainVerificationError(
                organization_id=requested_organization_id,
                seq=row.seq,
                kind=AuditChainFailureKind.CONTENT,
            )
        previous_curr_hash = curr_hash
        last_seq = row.seq
    return AuditChainVerificationResult(
        organization_id=requested_organization_id,
        row_count=len(rows),
        last_seq=last_seq,
    )


def _authorized_rows(
    database_connection: BaseDatabaseWrapper,
    organization_id: UUID | None,
) -> tuple[UUID, list[_AuditStoredRow]]:
    requested_organization_id = organization_id or SYSTEM_ORG_ID
    with database_connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, "
            "NULLIF(current_setting('app.current_tenant', true), '')"
        )
        context_row = cursor.fetchone()
        if context_row is None:
            raise AuditVerificationAccessRejectedError(
                organization_id=requested_organization_id,
                database_role="unknown",
            )
        database_role = str(context_row[0])
        if organization_id is None:
            if database_role != "clinic_owner":
                raise AuditVerificationAccessRejectedError(
                    organization_id=requested_organization_id,
                    database_role=database_role,
                )
            cursor.execute(
                """
                SELECT seq, organization_id, actor_user_id, event_type,
                       component_id, host(component_ip), affected_record_type,
                       affected_record_id, occurred_at_utc, payload::text,
                       prev_hash, curr_hash
                FROM clinic_app.audit_event
                WHERE organization_id = %s
                ORDER BY seq
                """,
                [SYSTEM_ORG_ID],
            )
        else:
            try:
                tenant_organization_id = UUID(str(context_row[1]))
            except (TypeError, ValueError):
                raise AuditVerificationAccessRejectedError(
                    organization_id=requested_organization_id,
                    database_role=database_role,
                ) from None
            if tenant_organization_id != organization_id:
                raise AuditVerificationAccessRejectedError(
                    organization_id=requested_organization_id,
                    database_role=database_role,
                )
            cursor.execute(
                """
                SELECT seq, organization_id, actor_user_id, event_type,
                       component_id, host(component_ip), affected_record_type,
                       affected_record_id, occurred_at_utc, payload::text,
                       prev_hash, curr_hash
                FROM clinic_app.audit_event_tenant
                WHERE organization_id = %s
                ORDER BY seq
                """,
                [organization_id],
            )
        return requested_organization_id, [
            _AuditStoredRow(*row) for row in cursor.fetchall()
        ]


def _content_hashes(
    row: _AuditStoredRow,
    organization_id: UUID,
    prev_hash: bytes,
) -> tuple[bytes, bytes]:
    try:
        component_ip = (
            None if row.component_ip is None else ip_address(row.component_ip)
        )
        event = AuditEventInput(
            event_type=row.event_type,
            component_id=row.component_id,
            component_ip=component_ip,
            affected_record_type=row.affected_record_type,
            affected_record_id=row.affected_record_id,
            occurred_at_utc=row.occurred_at_utc,
        )
        context = AuditTrustedContext(
            organization_id=UUID(str(row.organization_id)),
            actor_user_id=(
                None if row.actor_user_id is None else UUID(str(row.actor_user_id))
            ),
        )
        normalized_payload = _normalize_payload(json.loads(row.payload))
        content_hash = _content_hash(event, context, normalized_payload)
        curr_hash = bytes(row.curr_hash)
    except (
        AttributeError,
        AuditEventValueRejectedError,
        AuditPayloadKeyRejected,
        AuditPayloadValueRejectedError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        raise AuditChainVerificationError(
            organization_id=organization_id,
            seq=row.seq,
            kind=AuditChainFailureKind.CONTENT,
        ) from None
    return sha256(content_hash + prev_hash).digest(), curr_hash
