"""Retain sandbox PSP requests and authenticated reconciliation evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.billing.migrations._psp_schema import OPERATIONS
from apps.billing.migrations._psp_sql import SQL
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
    """Validate FKs before installing policies whose helpers exclude the owner."""
    referenced_tables = (
        "billing_journalbook",
        "billing_journalsettlement",
        "billing_settlementallocation",
        "identity_sessionrecord",
    )
    for table in referenced_tables:
        schema_editor.execute(relax_force_rls(table))
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    for table in referenced_tables:
        schema_editor.execute(restore_force_rls(table))


class Migration(migrations.Migration):
    """Append-only financial history is not rolled back by deleting its rows."""

    dependencies: ClassVar = [
        ("billing", "0011_operational_journal"),
        ("providers", "0001_initial"),
    ]
    operations: ClassVar[list[Operation]] = [
        # Each new model renders once instead of re-rendering the tenant graph.
        SchemaStateBatch(database_operations=OPERATIONS, state_operations=OPERATIONS),
        migrations.RunPython(finish_foreign_keys, reverse_code=None),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
