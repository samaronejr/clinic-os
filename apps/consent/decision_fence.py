"""Serialize an AI choice change with decisions on still-pending clinic work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, connections

if TYPE_CHECKING:
    from uuid import UUID


def _key(clinic_id: UUID) -> str:
    return f"ai-pending-decision:{clinic_id}"


def hold_choice_change(*, clinic_id: UUID) -> None:
    """Take the writer side before other consent locks, through commit."""
    if not connection.in_atomic_block:
        msg = "consent change requires a transaction"
        raise RuntimeError(msg)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", [_key(clinic_id)]
        )


def try_hold_decision(*, clinic_id: UUID, using: str) -> bool:
    """Refuse rather than wait for a writer while holding source or audit locks."""
    db = connections[using]
    if not db.in_atomic_block:
        return False
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock_shared(hashtextextended(%s,0))",
            [_key(clinic_id)],
        )
        result = cursor.fetchone()
        return result is not None and result[0] is True
