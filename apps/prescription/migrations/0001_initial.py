"""Create scoped drafts and retained item snapshots."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Keep prescription identity separate from SOAP-specific document fields."""

    initial = True

    dependencies: ClassVar = [
        ("ehr", "0008_finalization_policy"),
        ("identity", "0007_physician_verification_policy"),
        ("intake", "0009_teleconsult_operation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="PrescriptionDraft",
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
                ("category", models.CharField(max_length=40)),
                ("contract_version", models.CharField(max_length=64)),
                ("version", models.PositiveIntegerField(default=1)),
                ("state", models.CharField(default="draft", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "encounter",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
                    ),
                ),
                (
                    "issuer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
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
            name="PrescriptionItem",
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
                ("version", models.PositiveIntegerField()),
                ("position", models.PositiveSmallIntegerField()),
                ("medication_description", models.CharField(max_length=240)),
                ("strength_form", models.CharField(max_length=160)),
                ("dose", models.CharField(max_length=160)),
                ("route", models.CharField(max_length=80)),
                ("frequency", models.CharField(max_length=160)),
                ("duration", models.CharField(max_length=160)),
                ("quantity", models.CharField(max_length=80)),
                ("instructions", models.CharField(blank=True, max_length=2000)),
                (
                    "draft",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="prescription.prescriptiondraft",
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
                "ordering": ["position"],
            },
        ),
        migrations.AddConstraint(
            model_name="prescriptiondraft",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="prescription_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondraft",
            constraint=models.CheckConstraint(
                condition=models.Q(("state__in", ["draft", "discarded"])),
                name="prescription_draft_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondraft",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("category", "synthetic_non_controlled"),
                    ("contract_version", "synthetic-draft-v1"),
                ),
                name="prescription_synthetic_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptionitem",
            constraint=models.UniqueConstraint(
                fields=("draft", "version", "position"),
                name="prescription_item_version_position",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptionitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("position__gte", 1), ("position__lte", 20), ("version__gte", 1)
                ),
                name="prescription_item_bounds",
            ),
        ),
    ]
