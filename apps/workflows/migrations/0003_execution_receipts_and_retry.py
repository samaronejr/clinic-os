"""Forward-install current execution guards on the already migrated candidate."""

from typing import ClassVar

from django.db import migrations

from ._workflow_sql import SQL as INITIAL_SQL

_statements = ["SET LOCAL ROLE clinic_resolver;"]
for _signature in ("workflows_now()", "workflows_guard()"):
    _start = INITIAL_SQL.index(f"CREATE FUNCTION clinic_app.{_signature}")
    _end = INITIAL_SQL.index("$f$;", _start) + len("$f$;")
    _statements.append(
        INITIAL_SQL[_start:_end].replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
    )
_statements.append("""
REVOKE ALL ON FUNCTION clinic_app.workflows_now() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_now() TO clinic_app;
RESET ROLE;
GRANT UPDATE (revision,last_command_key,last_command_digest)
 ON clinic_app.workflows_workflowrun TO clinic_app;
""")
SQL = "\n".join(_statements)


class Migration(migrations.Migration):
    """Preserve history and pins; install stricter effects and explicit retries."""

    dependencies: ClassVar = [("workflows", "0002_predecessor_eligibility")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
