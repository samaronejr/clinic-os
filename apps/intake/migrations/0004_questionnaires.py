"""Install immutable questionnaire versions, clinical/patient RLS and receipts."""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._questionnaire_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Extend patient operations without elevating any existing session."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        ("intake", "0003_patient_access"),
        ("scheduling", "0002_appointment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="QuestionnaireEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("actor_id", models.UUIDField(null=True)),
                ("patient_session_id", models.UUIDField(null=True)),
                ("action", models.CharField(max_length=16)),
                ("revision", models.PositiveIntegerField()),
                ("answers", models.JSONField()),
                ("reason", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="QuestionnaireResponse",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("answers", models.JSONField(default=dict)),
                ("state", models.CharField(default="draft", max_length=16)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("reopen_reason", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="QuestionnaireTemplate",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("key", models.SlugField(max_length=64)),
                ("version", models.PositiveIntegerField()),
                ("title", models.CharField(max_length=160)),
                ("questions", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
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
                    (["enrollment_view", "questionnaires"],),
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
                    (["enrollment_view", "questionnaires"],),
                    output_field=models.BooleanField(),
                ),
                name="intake_session_operations_check",
            ),
        ),
        migrations.AddField(
            model_name="questionnaireevent",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="questionnaireevent",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="appointment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="scheduling.appointment",
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="enrollment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="intake.patientclinicenrollment",
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="patient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
            ),
        ),
        migrations.AddField(
            model_name="questionnaireevent",
            name="response",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="intake.questionnaireresponse",
            ),
        ),
        migrations.AddField(
            model_name="questionnairetemplate",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="questionnairetemplate",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="questionnaireresponse",
            name="template",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="intake.questionnairetemplate",
            ),
        ),
        migrations.AddConstraint(
            model_name="questionnairetemplate",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "key", "version"),
                name="intake_questionnaire_version_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="questionnairetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="intake_questionnaire_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="questionnaireresponse",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("state", "draft"), ("submitted_at__isnull", True)),
                    models.Q(("state", "submitted"), ("submitted_at__isnull", False)),
                    _connector="OR",
                ),
                name="intake_response_state_check",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
