"""Reserve each message's single send permit before any provider call."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations._send_permit_sql import BACKFILL_SQL, COLUMN_SQL, GUARD_SQL
from apps.tenancy.migrations._protected_migration import (
    irreversible,
    open_table_backfill,
)

_STATE = [
    migrations.AddField(
        model_name="integrationoperation",
        name="send_permit",
        field=models.CharField(editable=False, max_length=8, null=True),
    ),
    migrations.AddConstraint(
        model_name="integrationoperation",
        constraint=models.CheckConstraint(
            condition=models.Q(
                send_permit__in=("held", "released"), subject_type="comms.message"
            )
            | models.Q(send_permit__isnull=True),
            name="comms_operation_send_permit_check",
        ),
    ),
    migrations.AddConstraint(
        model_name="integrationoperation",
        constraint=models.UniqueConstraint(
            fields=("subject_type", "subject_id"),
            condition=models.Q(send_permit="held"),
            name="comms_operation_send_permit_uniq",
        ),
    ),
]


class Migration(migrations.Migration):
    """Non-atomic and irreversible: the permit backfill commits on its own."""

    atomic = False
    dependencies: ClassVar = [("comms", "0014_attempt_guard")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(COLUMN_SQL),
        migrations.RunPython(
            open_table_backfill(
                tables=(
                    "comms_integrationoperation",
                    "comms_message",
                    "comms_inboundevent",
                ),
                sql=BACKFILL_SQL,
            ),
            irreversible,
        ),
        migrations.RunSQL(GUARD_SQL),
    ]
