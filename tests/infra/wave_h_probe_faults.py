"""Collect one real, rollback-only row mutation per table inside each probe."""

from __future__ import annotations

import json
import os
import re
import sys
from runpy import run_path
from typing import TYPE_CHECKING

from django.db import DatabaseError, connection, transaction
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import FrameType

PROBES = ("_baseline_approvals", "_baseline_retained")


def _alternates(kind: str, nullable: bool) -> tuple[str, ...]:
    choices = ("NULL",) if nullable else ()
    if kind in {"character varying", "character", "text"}:
        return (*choices, "''", "'r8'", "'x'")
    if kind in {"integer", "smallint", "bigint", "numeric", "double precision"}:
        return (*choices, "{column} + 1", "{column} - 1")
    if kind in {"date", "timestamp with time zone", "timestamp without time zone"}:
        return (*choices, "{column} - interval '1 day'")
    if kind == "boolean":
        return (*choices, "NOT {column}")
    if kind == "jsonb":
        return (*choices, "'{{}}'::jsonb", "'[]'::jsonb")
    return choices


def _mutate(table: str) -> dict[str, object]:
    name = sql.Identifier("clinic_app", table)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
            "WHERE table_schema='clinic_app' AND table_name=%s AND column_name<>'id' "
            "ORDER BY ordinal_position",
            [table],
        )
        columns = cursor.fetchall()
        for column, kind, nullable in columns:
            expression = sql.Identifier(column)
            for alternate in _alternates(kind, nullable == "YES"):
                try:
                    with transaction.atomic():
                        # This is a disposable synthetic savepoint. Restore the
                        # exact original guard/row state before the probe runs.
                        cursor.execute(
                            sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(name)
                        )
                        cursor.execute(
                            sql.SQL("ALTER TABLE {} DISABLE ROW LEVEL SECURITY").format(
                                name
                            )
                        )
                        choice = sql.SQL(alternate).format(column=expression)
                        cursor.execute(
                            sql.SQL(
                                "UPDATE {table} t SET {column}={choice} "
                                "WHERE id=(SELECT id FROM {table} "
                                "WHERE {column} IS DISTINCT FROM {choice} "
                                "ORDER BY id LIMIT 1) RETURNING id::text,to_jsonb(t)"
                            ).format(table=name, column=expression, choice=choice)
                        )
                        mutated = cursor.fetchone()
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                        transaction.set_rollback(True)
                except DatabaseError:
                    continue
                if mutated is not None:
                    key, data = mutated
                    return {"rows": [(key, json.loads(data))], "column": column}
    message = f"no stored one-column mutation available for {table}"
    raise AssertionError(message)


def run_seed(seed: Path, output: Path) -> None:
    """Execute the exact seed and retain only test faults, not probe side effects."""
    if os.environ.get("CLINIC_DATA_MODE") != "synthetic":
        message = "probe faults require synthetic rehearsal"
        raise RuntimeError(message)
    seen: list[str] = []
    cases: dict[str, dict[str, dict[str, object]]] = {}

    def observe(
        execute: Callable[..., object],
        statement: object,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        if isinstance(statement, sql.Composable) and (
            match := re.fullmatch(
                r'SELECT id::text,to_jsonb\(t\) FROM clinic_app\."(\w+)" t ORDER BY id',
                statement.as_string(),
            )
        ):
            seen.append(match[1])
        return execute(statement, params, many, context)

    def probe(frame: FrameType, event: str, arg: object) -> None:
        phase = frame.f_code.co_name
        if (
            event == "call"
            and phase in PROBES
            and frame.f_code.co_filename.endswith("/migration_upgrade_ai_seed.py")
        ):
            assert phase not in cases
            assert len(seen) == len(set(seen)) == 59, (phase, len(seen))
            cases[phase] = {table: _mutate(table) for table in seen}

    previous = sys.getprofile()
    try:
        sys.setprofile(probe)
        with connection.execute_wrapper(observe):
            run_path(str(seed))
    finally:
        sys.setprofile(previous)
    assert tuple(cases) == PROBES
    assert len(seen) == 59
    output.write_text(json.dumps(cases), encoding="utf-8")
