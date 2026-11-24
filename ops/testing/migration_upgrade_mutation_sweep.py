"""Exercise every table of a real rehearsal from its unfiltered write set."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import Rollback, sql

from ops.testing.migration_upgrade_rows import (
    differences,
    fixture_coverage_failure,
    read_rows,
    table_keys,
)

if TYPE_CHECKING:
    from psycopg import Cursor


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


def run_sweep(seeded: set[str]) -> set[str]:
    """Mutate each seeded table under rollback and return verified coverage."""
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
        swept = set()
        for table in sorted(seeded):
            rows = expected[table]
            key = min(rows)
            column = next(
                col
                for col, value in sorted(rows[key].items())
                if col not in available[table] and value is not None
            )
            with admin.transaction() as mutation:
                _mutate(cursor, table, available[table], key, column)
                actual = read_rows(cursor)
                actual.pop("fixture_sweep_original")
                expected_failure = f"seeded value mismatch: {table}.{column} {key}"
                if differences(expected, actual) != [expected_failure]:
                    message = f"sweep did not isolate {table}.{column} {key}"
                    raise RuntimeError(message)
                swept.add(table)
                raise Rollback(mutation)
    if failure := fixture_coverage_failure(seeded, swept):
        raise RuntimeError(failure)
    return swept


def main() -> None:
    """Use the separately observed writes from one real migrated fixture."""
    inventory = json.loads(
        Path(os.environ["UPGRADE_SEED_WRITES"]).read_text(encoding="utf-8")
    )
    covered = run_sweep(set(inventory))
    for omitted in sorted(covered):
        if fixture_coverage_failure(set(inventory), covered - {omitted}) is None:
            message = f"sweep removal was not rejected: {omitted}"
            raise RuntimeError(message)
    print(f"upgrade mutation sweep: {len(covered)} tables verified")  # noqa: T201


if __name__ == "__main__":
    main()
