"""Release send permits only on recorded provider evidence."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations._permit_evidence_sql import BACKFILL_SQL, SQL
from apps.tenancy.migrations._protected_migration import (
    irreversible,
    open_table_backfill,
)

_STATE = [
    migrations.RemoveConstraint(
        model_name="integrationoperation", name="comms_operation_send_permit_check"
    ),
    migrations.AddConstraint(
        model_name="integrationoperation",
        constraint=models.CheckConstraint(
            condition=models.Q(
                send_permit__in=("held", "open", "released"),
                subject_type="comms.message",
            )
            | models.Q(send_permit__isnull=True),
            name="comms_operation_send_permit_check",
        ),
    ),
]


class Migration(migrations.Migration):
    """Non-atomic and irreversible: the evidence backfill commits on its own."""

    atomic = False
    dependencies: ClassVar = [("comms", "0015_send_permit")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(SQL),
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
    ]
