"""Add retained protected adjudications and reversible identity lineage.

Protected columns ship encrypted. This migration is non-atomic and
irreversible; rollback is an owned restore, never dropping retained history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

from apps.intake.migrations._alias_contract_sql import ALIAS_SQL
from apps.intake.migrations._merge_decision_operation import OPERATION
from apps.intake.migrations._patient_alias_operations import (
    ALIAS_OPERATION,
    TRANSFER_OPERATION,
)
from apps.tenancy.migration_operations import SeparateDatabaseAndState
from apps.tenancy.migrations._resumable_schema import GuardedMigration

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation


def install_alias_contract(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Install the protected contract in one owned DDL transaction."""
    del apps
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(ALIAS_SQL)


class Migration(GuardedMigration):
    """Preserve protected identity history; rollback requires restore."""

    dependencies: ClassVar = [
        ("intake", "0016_duplicate_detection"),
        ("identity", "0024_lifecycle_function_privileges"),
    ]
    _schema_operations: ClassVar[list[Operation]] = [
        OPERATION,
        ALIAS_OPERATION,
        TRANSFER_OPERATION,
        migrations.RunPython(install_alias_contract, atomic=True),
    ]
    operations: ClassVar[list[Operation]] = [
        SeparateDatabaseAndState(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
    ]
