"""Serialize an AI choice change with decisions on still-pending clinic work."""

from __future__ import annotations

from dataclasses import dataclass
from threading import local
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
    _remember(connection.alias, _Held("writer:" + _key(clinic_id)))


def try_hold_decision(*, clinic_id: UUID, using: str) -> bool:
    """Refuse rather than wait for a writer while holding source or audit locks."""
    return _try_shared(_key(clinic_id), using)


def try_hold_organization_read() -> bool:
    """Cover withdrawals in every source clinic of an organization-wide read."""
    if not connection.in_atomic_block:
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT NULLIF(current_setting('app.current_tenant',true),'')")
        row = cursor.fetchone()
    return bool(
        row and row[0] and _try_shared(f"ai-pending-export:{row[0]}", "default")
    )


@dataclass(frozen=True, slots=True)
class _Held:
    """A transaction-local admission marker, never permission to read a row."""

    name: str

    def __call__(self) -> None:
        """No work at commit: PostgreSQL releases the transaction lock itself."""


_candidates = local()


def clinic_marker(clinic_id: UUID, using: str) -> object | None:
    """Identify this exact acquisition; an equal key after rollback is a new lock."""
    db = connections[using]
    if db.in_atomic_block:
        for entry in db.run_on_commit:
            callback = entry[1]
            if isinstance(callback, _Held) and callback.name == _key(clinic_id):
                return callback
    return None


def _remember(using: str, held: _Held) -> None:
    db = connections[using]
    aliases: set[str] = getattr(_candidates, "aliases", set())
    aliases.add(using)
    _candidates.aliases = aliases
    db.on_commit(held)


def decision_fenced() -> bool:
    """Check for a live consent-fenced transaction on the acting thread.

    The hint is not authority: Django discards the actual transaction marker
    on rollback to the acquiring savepoint, outer rollback, commit and close.
    Check other active aliases too: choosing a different manager alias cannot
    release the calling reader's fence. No SQL or extra locks are needed.
    """
    aliases: set[str] | None = getattr(_candidates, "aliases", None)
    if not aliases:
        return False
    for name in tuple(aliases):
        db = connections[name]
        if db.in_atomic_block and any(
            isinstance(e[1], _Held) for e in db.run_on_commit
        ):
            return True
        aliases.discard(name)
    return False


def _try_shared(name: str, using: str) -> bool:
    db = connections[using]
    if not db.in_atomic_block:
        return False
    held = _Held(name)
    # Django discards these entries on rollback (including rollback to the
    # savepoint where the lock was acquired), on close, and on outer commit.
    # A request-scoped flag alone would survive savepoint rollback incorrectly.
    if any(
        isinstance(entry[1], _Held) and entry[1] == held for entry in db.run_on_commit
    ):
        return True
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('transaction_isolation')='read committed' "
            "AND pg_try_advisory_xact_lock_shared(hashtextextended(%s,0))",
            [name],
        )
        if cursor.fetchone() != (True,):
            return False
    _remember(using, held)
    return True
