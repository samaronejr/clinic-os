"""Add retained protected adjudications and reversible identity lineage.

Protected columns ship encrypted. This migration is non-atomic and
irreversible; rollback is an owned restore, never dropping retained history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

from apps.intake.migrations._alias_bindings_sql import (
    SQL as BINDINGS_SQL,
)
from apps.intake.migrations._alias_bindings_sql import composite_bindings
from apps.intake.migrations._alias_guard_sql import FINALIZE_SQL
from apps.intake.migrations._alias_guard_sql import SQL as GUARD_SQL
from apps.intake.migrations._alias_scope_sql import SQL as SCOPE_SQL
from apps.intake.migrations._merge_decision_operation import OPERATION
from apps.intake.migrations._patient_alias_operations import (
    ALIAS_OPERATION,
    TRANSFER_OPERATION,
)
from apps.intake.rls import apply_intake_rls
from apps.tenancy.migrations_support import revoke_default_dml

TABLES = (
    "intake_mergedecision",
    "intake_patientalias",
    "intake_patientmergetransfer",
)
ALIAS_SQL = (
    "".join(
        revoke_default_dml(table, ("SELECT", "INSERT"))
        + apply_intake_rls(table, "organization_id")
        for table in TABLES
    )
    + SCOPE_SQL
    + GUARD_SQL
    + BINDINGS_SQL
    + composite_bindings()
    + FINALIZE_SQL
)

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation


def install_alias_contract(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Install the protected contract in one owned DDL transaction."""
    del apps
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(ALIAS_SQL)


class Migration(migrations.Migration):
    """Preserve protected identity history; rollback requires restore."""

    atomic = False
    dependencies: ClassVar = [
        ("intake", "0016_duplicate_detection"),
        ("identity", "0024_lifecycle_function_privileges"),
    ]
    operations: ClassVar[list[Operation]] = [
        OPERATION,
        ALIAS_OPERATION,
        TRANSFER_OPERATION,
        migrations.RunPython(install_alias_contract, atomic=True),
    ]
