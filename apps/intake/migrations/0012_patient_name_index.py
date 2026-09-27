"""Add exact-search digests without altering the independent legacy registry.

The owner backfill reveals one name at a time only to derive its HMAC. It
never filters or sorts decrypted values and never persists plaintext. A
failed key lookup rolls back the index and RLS changes together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations, models

from apps.intake.patient_name_index import name_indexes
from apps.tenancy.envelope import reveal

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation


def backfill(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Populate digests for pre-existing encrypted names in the DDL transaction."""
    del apps
    with (
        schema_editor.connection.cursor() as scan,
        schema_editor.connection.cursor() as cursor,
    ):
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(
            "ALTER TABLE clinic_app.intake_patient DISABLE ROW LEVEL SECURITY"
        )
        cursor.execute("SELECT current_setting('app.current_tenant', true)")
        prior = cursor.fetchone()[0]
        scan.execute(
            "SELECT id, organization_id, full_name FROM clinic_app.intake_patient"
        )
        while row := scan.fetchone():
            patient_id, organization_id, envelope = row
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            name = reveal(
                purpose="intake.patient.full_name", envelope=bytes(envelope)
            ).decode("utf-8")
            cursor.execute(
                "UPDATE clinic_app.intake_patient SET full_name_index = %s "
                "WHERE id = %s",
                [name_indexes(name)[-1], patient_id],
            )
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [prior or ""]
        )
        cursor.execute(
            "ALTER TABLE clinic_app.intake_patient ENABLE ROW LEVEL SECURITY"
        )
        cursor.execute("ALTER TABLE clinic_app.intake_patient FORCE ROW LEVEL SECURITY")


class Migration(migrations.Migration):
    """Reversible derived-index addition; encrypted source columns are untouched."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("intake", "0011_protected_fields"),
        ("tenancy", "0007_blind_indexes"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="patient",
            name="full_name_index",
            field=models.BinaryField(
                max_length=32, editable=False, null=True, db_index=True
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
