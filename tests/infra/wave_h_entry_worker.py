"""Run the selected worktree's complete verifier on one projected read fault."""

from __future__ import annotations

import faulthandler
import io
import json
import os
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any

import django
from django.db import connection
from psycopg import sql
from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from collections.abc import Callable

VERDICT_TIMEOUT_SECONDS = 180


def _guarded_verify(entry: Path, fault: dict[str, Any]) -> dict[str, object]:
    # A stuck verifier must not keep the parent blocked on its reply pipe.
    faulthandler.dump_traceback_later(VERDICT_TIMEOUT_SECONDS, exit=True)
    try:
        return _verify(entry, fault)
    finally:
        faulthandler.cancel_dump_traceback_later()


def _verify(entry: Path, fault: dict[str, Any]) -> dict[str, object]:
    changed = 0
    table = fault.get("table")
    original = (
        sql.SQL("SELECT id::text,to_jsonb(t) FROM clinic_app.{} t ORDER BY id")
        .format(sql.Identifier(table))
        .as_string()
        if table is not None
        else None
    )

    def inject(
        execute: Callable[..., Any],
        statement: object,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        nonlocal changed
        if (
            table is not None
            and isinstance(statement, sql.Composable)
            and statement.as_string() == original
        ):
            rows = fault["rows"]
            clause = sql.SQL(" ").join(
                sql.SQL("WHEN id::text=%s THEN %s::jsonb") for _ in rows
            )
            statement = sql.SQL(
                "SELECT id::text,CASE {} ELSE to_jsonb(t) END "
                "FROM clinic_app.{} t ORDER BY id"
            ).format(clause, sql.Identifier(table))
            params = [value for key, row in rows for value in (key, Jsonb(row))]
            changed += 1
        return execute(statement, params, many, context)

    status = 0
    output = io.StringIO()
    attestation = Path(os.environ["UPGRADE_VERIFIED_FILE"])
    attestation.unlink(missing_ok=True)
    try:
        with (
            connection.execute_wrapper(inject),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            runpy.run_path(str(entry))
    except SystemExit as error:
        if error.code != 1:
            raise
        status = 1
    finally:
        connection.close()
    if fault and changed != 1:
        message = f"fault did not reach exactly one historical read: {changed}"
        raise RuntimeError(message)
    if status == 0 and "upgrade-check: all assertions passed" not in output.getvalue():
        message = "verifier returned without a success diagnostic"
        raise RuntimeError(message)
    if status == 1 and "upgrade-check FAILED:" not in output.getvalue():
        message = "verifier exited without a check failure"
        raise RuntimeError(message)
    return {
        "status": status,
        "attested": attestation.is_file(),
        "failures": [
            line
            for line in output.getvalue().splitlines()
            if line.startswith("upgrade-check FAILED:")
        ],
    }


def main() -> None:
    """Consume cases from stdin without importing the other worktree."""
    django.setup()
    entry = Path.cwd() / "ops/testing/migration_upgrade_verify.py"
    for line in sys.stdin:
        sys.stdout.write(json.dumps(_guarded_verify(entry, json.loads(line))) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
