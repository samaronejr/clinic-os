"""Bind represented enrollments without rewriting the authenticated identity."""

from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._delegate_session_sql import SQL


class Migration(migrations.Migration):
    """Install effective authority and its patient-surface adapters atomically."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("intake", "0013_patient_demographics"),
        ("identity", "0018_patient_delegate_grants"),
        ("retention", "0003_protected_fields"),
        ("teleconsult", "0003_teleconsult_fail"),
        ("scheduling", "0006_capacity_insert_authorization"),
        ("billing", "0009_protected_fields"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="patientsession",
            name="acting_for_enrollment",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="represented_sessions",
                to="intake.patientclinicenrollment",
            ),
        ),
        migrations.AddField(
            model_name="patientsession",
            name="delegate_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="identity.patientdelegategrant",
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="restricted_from_delegates",
            field=models.BooleanField(default=True),
        ),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
