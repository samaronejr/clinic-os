"""Additive protected schema; irreversible; rollback = restore."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations
from psycopg import sql

from apps.ai.migrations._state_batch import SchemaStateBatch

from . import _insurance_sql
from ._schema import MODELS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import StateApps


def finish_foreign_keys(
    _apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Validate deferred relationships inside the protected schema transaction."""
    targets = (
        "identity_organization",
        "identity_clinic",
        "identity_user",
        "billing_invoice",
        "billing_journalsettlement",
        "billing_settlementallocation",
        "ehr_encounter",
        "intake_insurancemembership",
        "intake_patientclinicenrollment",
        "scheduling_appointment",
        "workflows_task",
    )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='clinic_app' "
            "AND c.relname=ANY(%s) AND c.relforcerowsecurity",
            [list(targets)],
        )
        forced = [row[0] for row in cursor.fetchall()]
    for table in forced:
        schema_editor.execute(
            sql.SQL("ALTER TABLE clinic_app.{} NO FORCE ROW LEVEL SECURITY")
            .format(sql.Identifier(table))
            .as_string()
        )
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    for table in forced:
        schema_editor.execute(
            sql.SQL("ALTER TABLE clinic_app.{} FORCE ROW LEVEL SECURITY")
            .format(sql.Identifier(table))
            .as_string()
        )


class ProtectedSchema(SchemaStateBatch):
    """Publish all encrypted tables, state guards and least privileges atomically."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """No plaintext conversion and no reverse operation that can destroy history."""

    atomic = False
    initial = True
    dependencies: ClassVar = [
        ("identity", "0047_insurance_approval_permission"),
        ("billing", "0011_operational_journal"),
        ("ehr", "0016_later_pack_draft_guard"),
        ("intake", "0028_messages_operation"),
        ("workflows", "0005_result_source_tasks"),
        ("audit", "0005_clinic_metadata_v2"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        ProtectedSchema(
            database_operations=[
                *MODELS,
                migrations.RunPython(finish_foreign_keys, reverse_code=None),
                migrations.RunSQL(
                    _insurance_sql.GRANTS
                    + _insurance_sql.FUNCTIONS
                    + _insurance_sql.FUNCTION_PRIVILEGES
                    + _insurance_sql.posture()
                ),
            ],
            state_operations=MODELS,
        )
    ]
