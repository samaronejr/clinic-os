"""Assign signing order in the database and allow one settled operation."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.prescription.migrations._signature_sequence_sql import GUARD_SQL

_SETTLED = (
    "draft",
    "prepared",
    "signing",
    "signed_unverified",
    "issued",
    "rehearsal_complete",
)
_STATE = [
    migrations.AddConstraint(
        model_name="signatureoperation",
        constraint=models.UniqueConstraint(
            fields=("document", "attempt_sequence"),
            name="prescription_signature_sequence_unique",
        ),
    ),
    migrations.AddConstraint(
        model_name="signatureoperation",
        constraint=models.CheckConstraint(
            condition=models.Q(attempt_sequence__gt=0),
            name="prescription_signature_sequence_assigned",
        ),
    ),
    migrations.AddConstraint(
        model_name="signatureoperation",
        constraint=models.UniqueConstraint(
            fields=("document",),
            condition=models.Q(state__in=_SETTLED),
            name="prescription_signature_settled_unique",
        ),
    ),
]


class Migration(migrations.Migration):
    """A document holding two settled operations fails this migration closed."""

    dependencies: ClassVar = [("prescription", "0017_signature_sequence")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=[], state_operations=_STATE),
        migrations.RunSQL(GUARD_SQL),
    ]
