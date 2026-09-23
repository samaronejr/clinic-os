"""Permit billing in new invitations without upgrading existing patient grants."""

from typing import ClassVar

import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Extend only the closed operation vocabulary, leaving all prior rows intact."""

    dependencies: ClassVar = [
        ("identity", "0007_physician_verification_policy"),
        ("intake", "0009_teleconsult_operation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="patientaccessgrant",
            name="intake_grant_operations_check",
        ),
        migrations.RemoveConstraint(
            model_name="patientsession",
            name="intake_session_operations_check",
        ),
        migrations.AddConstraint(
            model_name="patientaccessgrant",
            constraint=models.CheckConstraint(
                condition=django.db.models.expressions.RawSQL(
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
                            "billing",
                        ],
                    ),
                    output_field=models.BooleanField(),
                ),
                name="intake_grant_operations_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="patientsession",
            constraint=models.CheckConstraint(
                condition=django.db.models.expressions.RawSQL(
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
                            "billing",
                        ],
                    ),
                    output_field=models.BooleanField(),
                ),
                name="intake_session_operations_check",
            ),
        ),
    ]
