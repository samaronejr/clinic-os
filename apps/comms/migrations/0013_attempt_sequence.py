"""Number every existing message attempt, resumably, once."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations._attempt_sequence_sql import BACKFILL_SQL, COLUMN_SQL
from apps.tenancy.migrations._protected_migration import (
    irreversible,
    open_table_backfill,
)

_STATE = [
    migrations.AddField(
        model_name="integrationoperation",
        name="attempt_sequence",
        field=models.BigIntegerField(editable=False, null=True),
    ),
]


class Migration(migrations.Migration):
    """Non-atomic and irreversible: the numbering pass commits on its own."""

    atomic = False
    dependencies: ClassVar = [("comms", "0012_attempt_delivery")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(COLUMN_SQL),
        migrations.RunPython(
            open_table_backfill(table="comms_integrationoperation", sql=BACKFILL_SQL),
            irreversible,
        ),
    ]
