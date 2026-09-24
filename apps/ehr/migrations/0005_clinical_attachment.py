"""Create quarantined clinical attachment metadata rows."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Introduce the task-23 attachment schema; bytes live in object storage."""

    dependencies: ClassVar = [
        ("ehr", "0004_history_policy"),
        ("identity", "0005_clinic_timezone"),
        (
            "intake",
            "0006_remove_patientchannelpreference_intake_preference_purpose_check_and_more",
        ),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="ClinicalAttachment",
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
                ("storage_key", models.CharField(max_length=64, unique=True)),
                ("file_name", models.CharField(max_length=120)),
                ("declared_type", models.CharField(max_length=64)),
                ("detected_type", models.CharField(max_length=64)),
                ("size_bytes", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("quarantined", "Em verificação"),
                            ("available", "Disponível"),
                            ("rejected", "Rejeitado"),
                        ],
                        default="quarantined",
                        max_length=16,
                    ),
                ),
                ("scan_attempts", models.PositiveIntegerField(default=0)),
                (
                    "scan_reason",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("scanned_at", models.DateTimeField(blank=True, null=True)),
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
                (
                    "uploader",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="clinicalattachment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("state__in", ["quarantined", "available", "rejected"])
                ),
                name="ehr_attachment_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicalattachment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("size_bytes__gte", 1), ("size_bytes__lte", 10485760)
                ),
                name="ehr_attachment_size_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicalattachment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "declared_type__in",
                        ["application/pdf", "image/jpeg", "image/png"],
                    )
                )
                & models.Q(
                    (
                        "detected_type__in",
                        ["application/pdf", "image/jpeg", "image/png"],
                    )
                ),
                name="ehr_attachment_type_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicalattachment",
            constraint=models.CheckConstraint(
                condition=models.Q(("sha256__regex", "^[0-9a-f]{64}$"))
                & models.Q(("storage_key__regex", "^[0-9a-f]{64}$")),
                name="ehr_attachment_digest_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicalattachment",
            constraint=models.CheckConstraint(
                condition=models.Q(scanned_at__isnull=True, state="quarantined")
                | models.Q(
                    scanned_at__isnull=False,
                    state__in=["available", "rejected"],
                ),
                name="ehr_attachment_scanned_check",
            ),
        ),
    ]
