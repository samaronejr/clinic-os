"""Permit portal messages in new invitations without upgrading issued grants."""

from collections.abc import Sequence
from typing import ClassVar, Final

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._state_batch import SchemaStateBatch

OPERATIONS: Final = [
    "enrollment_view",
    "questionnaires",
    "booking",
    "records",
    "consent",
    "teleconsult",
    "billing",
    "messages",
]
# One state batch: only the two grant models re-render, never the tenant graph.
_SCHEMA: Final[Sequence[Operation]] = [
    operation
    for model, name in (
        ("patientaccessgrant", "intake_grant_operations_check"),
        ("patientsession", "intake_session_operations_check"),
    )
    for operation in (
        migrations.RemoveConstraint(model_name=model, name=name),
        migrations.AddConstraint(
            model_name=model,
            constraint=models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "operations::pg_catalog.text[] <@ %s::text[] AND "
                    "pg_catalog.array_length(operations, 1) > 0",
                    (OPERATIONS,),
                    output_field=models.BooleanField(),
                ),
                name=name,
            ),
        ),
    )
]


class Migration(migrations.Migration):
    """Extend only the closed patient-operation vocabulary; no row changes."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("intake", "0027_portal_reads"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        SchemaStateBatch(database_operations=_SCHEMA, state_operations=_SCHEMA),
    ]
