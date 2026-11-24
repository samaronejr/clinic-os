"""Bind each in-flight send to its claim; only the claim records its outcome."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations._inflight_claim_sql import SQL

_STATE = [
    migrations.AddField(
        model_name="integrationoperation",
        name="send_permit_claim",
        field=models.BinaryField(editable=False, null=True),
    ),
]


class Migration(migrations.Migration):
    """Additive column, guard and resolvers; rollback requires restore."""

    dependencies: ClassVar = [("comms", "0016_permit_evidence")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(SQL),
    ]
