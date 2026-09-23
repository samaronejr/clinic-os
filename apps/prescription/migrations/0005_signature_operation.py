"""Create the explicit signature lifecycle and authenticated callback rows."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Keep signing state separate from the immutable rendered artifact."""

    dependencies: ClassVar = [
        ("prescription", "0004_document_policy"),
        ("identity", "0007_physician_verification_policy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="SignatureOperation",
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
                ("provider", models.CharField(max_length=64)),
                ("operation_id", models.CharField(default="", max_length=128)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("draft", "Rascunho"),
                            ("prepared", "Preparado"),
                            ("signing", "Assinando"),
                            ("issued", "Emitido"),
                            ("rehearsal_complete", "Ensaio concluído"),
                            ("failed", "Falhou"),
                        ],
                        default="draft",
                        max_length=24,
                    ),
                ),
                ("content_digest", models.CharField(max_length=64)),
                ("signer_subject", models.CharField(max_length=255)),
                ("evidence_snapshot", models.JSONField(null=True)),
                ("authorized_until", models.DateTimeField(null=True)),
                ("signed_bytes", models.BinaryField(null=True)),
                ("signed_digest", models.CharField(max_length=64, null=True)),
                ("failure_reason", models.CharField(default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(null=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="signature_operations",
                        to="prescription.prescriptiondocument",
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
                    "evidence",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.physicianevidence",
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
        migrations.CreateModel(
            name="SignatureCallback",
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
                ("event_id", models.CharField(max_length=128)),
                ("payload", models.JSONField()),
                ("verified_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="callbacks",
                        to="prescription.signatureoperation",
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
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(state__in=["draft", "prepared", "signing"]),
                name="prescription_signature_live_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.UniqueConstraint(
                fields=("provider", "operation_id"),
                condition=~models.Q(operation_id=""),
                name="prescription_signature_operation_id_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "draft",
                        "prepared",
                        "signing",
                        "issued",
                        "rehearsal_complete",
                        "failed",
                    ]
                ),
                name="prescription_signature_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(content_digest__regex="^[0-9a-f]{64}$"),
                name="prescription_signature_content_digest",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(signed_digest__regex="^[0-9a-f]{64}$")
                | models.Q(signed_digest__isnull=True),
                name="prescription_signature_signed_digest",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=["issued", "rehearsal_complete"],
                        signed_bytes__isnull=False,
                        signed_digest__isnull=False,
                        completed_at__isnull=False,
                    )
                    | ~models.Q(state__in=["issued", "rehearsal_complete"])
                ),
                name="prescription_signature_issued_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(state="failed", completed_at__isnull=False)
                    | ~models.Q(state="failed")
                ),
                name="prescription_signature_failed_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(state__in=["signing", "issued", "rehearsal_complete"])
                    & ~models.Q(operation_id="")
                )
                | models.Q(state__in=["draft", "prepared", "failed"]),
                name="prescription_signature_operation_id_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="signaturecallback",
            constraint=models.UniqueConstraint(
                fields=("operation", "event_id"),
                name="prescription_signature_callback_event_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="signaturecallback",
            constraint=models.CheckConstraint(
                condition=~models.Q(event_id=""),
                name="prescription_signature_callback_event_nonblank",
            ),
        ),
    ]
