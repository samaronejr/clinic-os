"""Fiscal reviews and artifacts are irreversible; rollback = restore."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

from apps.billing.migrations._fiscal_schema import OPERATIONS
from apps.billing.migrations._fiscal_sql import SQL

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState


class ProtectedFiscalSchema(migrations.SeparateDatabaseAndState):
    """Publish protected fields, FORCE RLS and exact grants in one transaction."""

    atomic = True
    reversible = False

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Install validated FKs on empty new tables before publishing FORCE RLS."""
        prior = schema_editor.sql_create_inline_fk
        schema_editor.sql_create_inline_fk = (
            "REFERENCES %(to_table)s (%(to_column)s) DEFERRABLE INITIALLY DEFERRED"
        )
        try:
            super().database_forwards(app_label, schema_editor, from_state, to_state)
        finally:
            schema_editor.sql_create_inline_fk = prior


class Migration(migrations.Migration):
    """Add fiscal history without changing receipts, invoices or journal facts."""

    atomic = False
    dependencies: ClassVar = [
        ("billing", "0011_operational_journal"),
    ]
    operations: ClassVar[list[Operation]] = [
        ProtectedFiscalSchema(
            database_operations=[*OPERATIONS, migrations.RunSQL(SQL)],
            state_operations=OPERATIONS,
        )
    ]
