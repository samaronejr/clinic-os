"""Number every existing signing operation per document, resumably, once."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.prescription.migrations._signature_sequence_sql import (
    BACKFILL_SQL,
    COLUMN_SQL,
)
from apps.tenancy.migrations._protected_migration import (
    irreversible,
    open_table_backfill,
)

_STATE = [
    migrations.AddField(
        model_name="signatureoperation",
        name="attempt_sequence",
        field=models.BigIntegerField(db_default=0, editable=False),
    ),
]


class Migration(migrations.Migration):
    """Non-atomic and irreversible: the numbering pass commits on its own."""

    atomic = False
    dependencies: ClassVar = [("prescription", "0016_dispatch_custody")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(COLUMN_SQL),
        migrations.RunPython(
            open_table_backfill(
                table="prescription_signatureoperation", sql=BACKFILL_SQL
            ),
            irreversible,
        ),
    ]
