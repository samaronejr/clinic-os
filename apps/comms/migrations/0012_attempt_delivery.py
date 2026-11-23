"""Retain authenticated attempt evidence without inferring legacy references."""

from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations._attempt_delivery_sql import SQL

_STATE = [
    migrations.AddField(
        model_name="inboundevent",
        name="operation",
        field=models.ForeignKey(
            null=True,
            on_delete=django.db.models.deletion.PROTECT,
            to="comms.integrationoperation",
        ),
    ),
    migrations.AddField(
        model_name="inboundevent",
        name="delivery_state",
        field=models.CharField(blank=True, db_default="", default="", max_length=16),
    ),
    migrations.AddConstraint(
        model_name="inboundevent",
        constraint=models.CheckConstraint(
            condition=models.Q(operation__isnull=True, delivery_state="")
            | models.Q(
                kind="status",
                operation__isnull=False,
                delivery_state__in=("sent", "delivered", "read", "failed"),
            ),
            name="comms_inbound_attempt_status",
        ),
    ),
]


class Migration(migrations.Migration):
    """Existing unbound receipts remain historical; rollback requires restore."""

    dependencies: ClassVar = [("comms", "0011_agent_replies")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=_STATE, state_operations=_STATE),
        migrations.RunSQL(SQL),
    ]
