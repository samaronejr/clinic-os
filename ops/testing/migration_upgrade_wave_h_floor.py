"""The Wave H per-primary-key projection and digest, kept as a verifier floor."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, TypedDict

from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from django.db.backends.utils import CursorWrapper


class Snapshot(TypedDict):
    """Original column names, digests by primary key, and exact row count."""

    columns: list[str]
    rows: dict[str, str]
    count: int


def _records(cursor: CursorWrapper, table: str) -> list[tuple[str, dict[str, object]]]:
    if table.startswith("analytics_"):
        cursor.execute("SET LOCAL ROLE clinic_resolver")
    cursor.execute(
        sql.SQL("SELECT id::text,to_jsonb(t) FROM clinic_app.{} t ORDER BY id").format(
            sql.Identifier(table)
        )
    )
    records = [(key, json.loads(raw)) for key, raw in cursor.fetchall()]
    cursor.execute("RESET ROLE")
    return records


def _digest(record: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def capture_floor(cursor: CursorWrapper, tables: Iterable[str]) -> dict[str, Snapshot]:
    """Capture seed digests at the original per-table guard restoration.

    No later capture point or column mapping is allowed for the Wave H floor.
    Probe outcomes are separate receipt fields, not replacements for this floor.
    """
    snapshots = {}
    for table in sorted(tables):
        records = _records(cursor, table)
        if not records:
            message = f"Wave H floor has no seed rows: {table}"
            raise RuntimeError(message)
        snapshots[table] = Snapshot(
            columns=sorted(records[0][1]),
            rows={key: _digest(record) for key, record in records},
            count=len(records),
        )
    return snapshots


def preserved(cursor: CursorWrapper, tables: dict[str, Snapshot]) -> dict[str, bool]:
    """Recompute Wave H's original-column digest and exact PK set/count."""
    return {
        f"preserved:{table}": records_preserved(snapshot, _records(cursor, table))
        for table, snapshot in tables.items()
    }


def records_preserved(
    snapshot: Snapshot,
    records: Iterable[tuple[str, Mapping[str, object]]],
) -> bool:
    """Apply the historical verifier's PK, column and digest contract."""
    required = set(snapshot["columns"])
    expected = snapshot["rows"]
    seen: set[str] = set()
    for key, record in records:
        if (
            key in seen
            or key not in expected
            or not record.keys() >= required
            or _digest({column: record[column] for column in required}) != expected[key]
        ):
            return False
        seen.add(key)
    return len(seen) == snapshot["count"] and seen == expected.keys()
