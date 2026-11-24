"""Exercise every table of a real rehearsal from its unfiltered write set."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import Rollback, sql

from ops.testing.migration_upgrade_fixture_oracle import check_fixtures
from ops.testing.migration_upgrade_rows import (
    _DEFAULT_COLUMNS,
    _REWRITES,
    fixture_coverage_failure,
    read_rows,
    table_keys,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from psycopg import Connection, Cursor


def _oracle_rejects(
    admin: Connection[tuple[object, ...]], required: Collection[str]
) -> bool:
    try:
        with admin.transaction():
            return bool(check_fixtures(required, database=admin).failures)
    except (psycopg.DatabaseError, RuntimeError) as error:
        if (
            getattr(error, "sqlstate", None) in {"TEN01", "TEN02", "TEN03"}
            or str(error).startswith("core fixture mismatch:")
        ):
            return True
        raise


def _mutate(
    cursor: Cursor[tuple[object, ...]],
    table: str,
    keys: list[str],
    key: str,
    column: str,
) -> None:
    # Shadow constraints only inside a rollback-only savepoint; this tests
    # the reader rather than whether a production guard blocks the mutation.
    original = sql.Identifier("clinic_app", table)
    shadow = sql.Identifier("fixture_sweep_original")
    cursor.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(original, shadow))
    cursor.execute(
        sql.SQL("CREATE TABLE {} AS SELECT * FROM clinic_app.{}").format(
            original, shadow
        )
    )
    cursor.execute(
        sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(
            original, sql.SQL(", ").join(map(sql.Identifier, keys))
        )
    )
    cursor.execute(
        sql.SQL("UPDATE {} AS t SET {}=NULL WHERE {}").format(
            original,
            sql.Identifier(column),
            sql.SQL(" AND ").join(
                sql.SQL("to_jsonb(t)->{} = %s::jsonb").format(sql.Literal(name))
                for name in keys
            ),
        ),
        [json.dumps(value) for value in json.loads(key)],
    )
    if cursor.rowcount != 1:
        message = f"sweep mutation did not select one row: {table}.{column}"
        raise RuntimeError(message)


def run_sweep(seeded: set[str], required: Collection[str]) -> set[str]:
    """Require the verdict oracle to reject a real mutation in each table."""
    rewritten = {
        (table, column)
        for entry in _DEFAULT_COLUMNS.values()
        for table, columns in entry.items()
        for column in columns
    } | {
        (table, column)
        for paths, _ in _REWRITES.values()
        for path in paths
        for table, column in [path.split(".", 1)]
    }
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        expected = read_rows(cursor)
        available = {
            table: keys for table, keys in table_keys(cursor) if table in expected
        }
        if failure := fixture_coverage_failure(seeded, set(available) & seeded):
            raise RuntimeError(failure)
        if failures := check_fixtures(required, database=admin).failures:
            message = f"sweep baseline failed: {failures}"
            raise RuntimeError(message)
        swept: set[str] = set()
        for table in sorted(seeded):
            rows = expected[table]
            columns: dict[str, str] = {}
            for key, row in sorted(rows.items()):
                for column, value in sorted(row.items()):
                    if (
                        value is not None
                        and column not in available[table]
                        and (table, column) not in rewritten
                    ):
                        columns.setdefault(column, key)
            for column, key in columns.items():
                with admin.transaction() as mutation:
                    _mutate(cursor, table, available[table], key, column)
                    rejected = _oracle_rejects(admin, required)
                    raise Rollback(mutation)
                if rejected:
                    swept.add(table)
                    break
    if failure := fixture_coverage_failure(seeded, swept):
        raise RuntimeError(failure)
    return swept


def main() -> None:
    """Use the separately observed writes from one real migrated fixture."""
    inventory = json.loads(
        Path(os.environ["UPGRADE_SEED_WRITES"]).read_text(encoding="utf-8")
    )
    receipt = json.loads(
        Path(os.environ["UPGRADE_SEED_FILE"]).read_text(encoding="utf-8")
    )
    required = {"core", *(name for name in ("ai", "inbox") if name in receipt)}
    covered = run_sweep(set(inventory), required)
    for omitted in sorted(covered):
        if fixture_coverage_failure(set(inventory), covered - {omitted}) is None:
            message = f"sweep removal was not rejected: {omitted}"
            raise RuntimeError(message)
    print(f"upgrade mutation sweep: {len(covered)} tables verified")  # noqa: T201


if __name__ == "__main__":
    main()
