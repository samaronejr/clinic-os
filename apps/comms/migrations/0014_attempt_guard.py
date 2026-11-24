"""Assign message attempt order in the database and gate every successor."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations._attempt_sequence_sql import GUARD_SQL

_STATE = [
    migrations.AddConstraint(
        model_name="integrationoperation",
        constraint=models.UniqueConstraint(
            fields=("subject_type", "subject_id", "attempt_sequence"),
            name="comms_operation_attempt_sequence_uniq",
        ),
    ),
    migrations.AddConstraint(
        model_name="integrationoperation",
        constraint=models.CheckConstraint(
            condition=models.Q(subject_type="comms.message")
            | models.Q(attempt_sequence__isnull=True),
            name="comms_operation_attempt_sequence_scope",
        ),
    ),
]


class Migration(migrations.Migration):
    """Additive constraint and trigger; rollback requires restore."""

    dependencies: ClassVar = [("comms", "0013_attempt_sequence")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(GUARD_SQL),
    ]
