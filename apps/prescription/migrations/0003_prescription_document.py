"""Create the immutable rendered prescription artifact."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Keep artifact identity separate from draft content rows."""

    dependencies: ClassVar = [
        ("prescription", "0002_draft_policy"),
        ("ehr", "0008_finalization_policy"),
        ("identity", "0007_physician_verification_policy"),
        ("intake", "0009_teleconsult_operation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="PrescriptionDocument",
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
                ("document_version", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[("rendered", "Renderizado")],
                        default="rendered",
                        max_length=16,
                    ),
                ),
                ("render_params", models.JSONField()),
                ("frozen_input", models.JSONField()),
                ("input_digest", models.CharField(max_length=64)),
                ("pdf_digest", models.CharField(max_length=64)),
                ("pdf_bytes", models.BinaryField()),
                ("qr_handle", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "draft",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="prescription.prescriptiondraft",
                    ),
                ),
                (
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.encounter",
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
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocument",
            constraint=models.UniqueConstraint(
                fields=("draft", "document_version"),
                name="prescription_document_version_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("document_version__gte", 1)),
                name="prescription_document_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("state__in", ["rendered"])),
                name="prescription_document_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("input_digest__regex", "^[0-9a-f]{64}$"))
                & models.Q(("pdf_digest__regex", "^[0-9a-f]{64}$")),
                name="prescription_document_digests",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("qr_handle__regex", "^[A-Za-z0-9_-]{32,64}$")),
                name="prescription_document_handle_shape",
            ),
        ),
    ]
