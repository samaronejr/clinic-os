"""Add a sealed clinic journal without rewriting historical financial rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

from apps.billing.migrations._journal_schema import OPERATIONS
from apps.billing.migrations._journal_sql import SQL

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
        schema_editor.execute(
            f"ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY"
        )
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    for table in legacy_tables:
        schema_editor.execute(
            f"ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY"
        )


class Migration(migrations.Migration):
    """Retain durable accounting history; rollback after posting is a restore."""

    dependencies: ClassVar = [
        ("billing", "0010_delegate_charges"),
        ("identity", "0043_delegate_messages_operation"),
        ("scheduling", "0010_patient_service_booking"),
    ]
    operations: ClassVar[list[Operation]] = [
        *OPERATIONS,
        migrations.RunPython(finish_foreign_keys, reverse_code=None),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
