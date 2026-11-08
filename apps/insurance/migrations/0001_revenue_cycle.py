"""Additive protected schema; irreversible; rollback = restore."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations

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
    # Deferred relationships reach these FORCE RLS tables; identity_user, the
    # remaining target, does not force RLS. Any other state fails closed, so the
    # literal toggles below never force a table that was not forced.
    forced_targets = (
        "billing_invoice",
        "billing_journalsettlement",
        "billing_settlementallocation",
        "ehr_encounter",
        "identity_clinic",
        "identity_organization",
        "intake_insurancemembership",
        "intake_patientclinicenrollment",
        "scheduling_appointment",
        "workflows_task",
    )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='clinic_app' "
            "AND c.relname=ANY(%s) AND c.relforcerowsecurity ORDER BY c.relname",
            [[*forced_targets, "identity_user"]],
        )
        forced = tuple(row[0] for row in cursor.fetchall())
    if forced != forced_targets:
        message = "insurance relationship targets changed their FORCE RLS state"
        raise RuntimeError(message)
    for table in forced_targets:
        schema_editor.execute(
            f"ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY"
        )
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    for table in forced_targets:
        schema_editor.execute(
            f"ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY"
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
