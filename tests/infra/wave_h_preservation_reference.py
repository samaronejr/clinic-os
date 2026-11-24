"""Frozen Wave H 4f8d2454 `_preserved` for the differential test."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from psycopg import sql

if TYPE_CHECKING:
    from django.db.backends.utils import CursorWrapper
    from ops.testing.migration_upgrade_wave_h_floor import Snapshot as _Snapshot


def _preserved(cursor: CursorWrapper, tables: dict[str, _Snapshot]) -> dict[str, bool]:
    assertions: dict[str, bool] = {}
    for table, snapshot in tables.items():
        if table.startswith("analytics_"):
            cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            sql.SQL(
                "SELECT id::text,to_jsonb(t) FROM clinic_app.{} t ORDER BY id"
            ).format(sql.Identifier(table))
        )
        actual = {}
        for key, raw in cursor.fetchall():
            record = json.loads(raw)
            actual[key] = hashlib.sha256(
                json.dumps(
                    {column: record[column] for column in snapshot["columns"]},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        assertions["preserved:" + table] = (
            actual == snapshot["rows"] and len(actual) == snapshot["count"]
        )
        cursor.execute("RESET ROLE")
    cursor.execute(
        "SELECT count(*) FROM pg_constraint WHERE connamespace="
        "'clinic_app'::regnamespace AND NOT convalidated "
        "AND conrelid=ANY(%s::regclass[])",
        [["clinic_app." + table for table in tables]],
    )
    assertions["all_seeded_constraints_validated"] = cursor.fetchone() == (0,)
    cursor.execute(
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgenabled<>'O' AND tgrelid=ANY(%s::regclass[])",
        [["clinic_app." + table for table in tables]],
    )
    assertions["all_seeded_guards_enabled"] = cursor.fetchone() == (0,)
    return assertions
