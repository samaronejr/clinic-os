"""Permit teleconsult grants without upgrading already issued invitations."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Extend only the closed patient-operation vocabulary."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        (
            "intake",
            "0008_remove_patientaccessgrant_intake_grant_operations_check_and_more",
        )
    ]
    operations: ClassVar[Sequence[Operation]] = [
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
                        (
                            [
                                "enrollment_view",
                                "questionnaires",
                                "booking",
                                "records",
                                "consent",
                                "teleconsult",
                            ],
                        ),
                        output_field=models.BooleanField(),
                    ),
                    name=name,
                ),
            ),
        )
    ]
