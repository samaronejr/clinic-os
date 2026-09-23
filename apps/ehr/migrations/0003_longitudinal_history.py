"""Create immutable longitudinal history assessments and entry versions."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Introduce the task-22 problem and allergy schema."""

    dependencies: ClassVar = [
        ("ehr", "0002_clinical_policy"),
        ("identity", "0005_clinic_timezone"),
        (
            "intake",
            "0006_remove_patientchannelpreference_intake_preference_purpose_check_and_more",
        ),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="HistoryAssessment",
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
                (
                    "kind",
                    models.CharField(
                        choices=[("problem", "Problemas"), ("allergy", "Alergias")],
                        max_length=16,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("not_assessed", "Não avaliado"),
                            ("none_documented", "Nenhum registro documentado"),
                            ("documented", "Registros documentados"),
                        ],
                        max_length=24,
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("author_label", models.CharField(max_length=150)),
                ("reason", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Allergy",
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
                ("entry_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("version", models.PositiveIntegerField()),
                ("description", models.CharField(max_length=1000)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Ativo"),
                            ("resolved", "Resolvido"),
                            ("entered_in_error", "Registrado por engano"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "assessment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.historyassessment",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Problem",
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
                ("entry_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("version", models.PositiveIntegerField()),
                ("description", models.CharField(max_length=1000)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Ativo"),
                            ("resolved", "Resolvido"),
                            ("entered_in_error", "Registrado por engano"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "assessment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.historyassessment",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="historyassessment",
            constraint=models.UniqueConstraint(
                fields=("clinic", "patient", "kind", "revision"),
                name="ehr_history_revision_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="historyassessment",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1)),
                name="ehr_history_revision_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="historyassessment",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["problem", "allergy"])),
                name="ehr_history_kind_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="historyassessment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("state__in", ["not_assessed", "none_documented", "documented"])
                ),
                name="ehr_history_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="allergy",
            constraint=models.UniqueConstraint(
                fields=("entry_id", "version"), name="allergy_entry_version_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="allergy",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)), name="allergy_version_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="allergy",
            constraint=models.CheckConstraint(
                condition=models.Q(("description", ""), _negated=True),
                name="allergy_description_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="allergy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["active", "resolved", "entered_in_error"])
                ),
                name="allergy_status_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="problem",
            constraint=models.UniqueConstraint(
                fields=("entry_id", "version"), name="problem_entry_version_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="problem",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)), name="problem_version_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="problem",
            constraint=models.CheckConstraint(
                condition=models.Q(("description", ""), _negated=True),
                name="problem_description_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="problem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["active", "resolved", "entered_in_error"])
                ),
                name="problem_status_check",
            ),
        ),
    ]
