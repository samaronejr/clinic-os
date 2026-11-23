"""Add a sealed clinic journal without rewriting historical financial rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.billing.migrations._journal_schema import OPERATIONS
from apps.billing.migrations._journal_sql import SQL
from apps.billing.migrations._relationship_validation_sql import (
    relax_force_rls,
    restore_force_rls,
)

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import StateApps


def finish_foreign_keys(
    _apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Validate deferred FKs before installing the new journal's forced policies."""
    legacy_tables = ("billing_invoice", "billing_settlement")
    for table in legacy_tables:
        schema_editor.execute(relax_force_rls(table))
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    for table in legacy_tables:
        schema_editor.execute(restore_force_rls(table))


class Migration(migrations.Migration):
    """Retain durable accounting history; rollback after posting is a restore."""

    dependencies: ClassVar = [
        ("billing", "0010_delegate_charges"),
        ("identity", "0043_delegate_messages_operation"),
        ("scheduling", "0010_patient_service_booking"),
    ]
    operations: ClassVar[list[Operation]] = [
        # Each new model renders once instead of re-rendering the tenant graph.
        SchemaStateBatch(database_operations=OPERATIONS, state_operations=OPERATIONS),
        migrations.RunPython(finish_foreign_keys, reverse_code=None),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
