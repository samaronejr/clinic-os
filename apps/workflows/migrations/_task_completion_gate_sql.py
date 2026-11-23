"""Frozen SQL for the task completion gate migration."""

from ._workflow_sql import SQL as INITIAL_SQL

_start = INITIAL_SQL.index("CREATE FUNCTION clinic_app.workflows_guard()")
_end = INITIAL_SQL.index("$f$;", _start) + len("$f$;")
SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + INITIAL_SQL[_start:_end].replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    )
    + "\nRESET ROLE;"
)
