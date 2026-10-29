"""Forward-install stored task completion and waiting receipt guards."""

from typing import ClassVar

from django.db import migrations

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


class Migration(migrations.Migration):
    """Keep existing receipt IDs, fences and publication pins intact."""

    dependencies: ClassVar = [("workflows", "0003_execution_receipts_and_retry")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
