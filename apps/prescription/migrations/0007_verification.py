"""Create revocation, patient release and anonymous probe tables."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Keep verification state separate from immutable signed bytes."""

    dependencies: ClassVar = [
        ("prescription", "0006_signature_policy"),
        ("ehr", "0008_finalization_policy"),
        ("identity", "0007_physician_verification_policy"),
        ("intake", "0009_teleconsult_operation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="PrescriptionDocumentRevocation",
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
                    "reason",
                    models.CharField(
                        choices=[
                            ("clinical_error", "Erro clínico"),
                            ("issuance_error", "Erro de emissão"),
                            ("issuer_request", "Solicitação do emissor"),
                            ("patient_request", "Solicitação do paciente"),
                        ],
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "document",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
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
                    "issuer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
                (
                    "revoked_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="prescription_revocations_made",
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
            ],
        ),
        migrations.CreateModel(
            name="PrescriptionDocumentRelease",
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
                ("revoked_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
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
                        related_name="releases",
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
                    "issuer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
                (
                    "released_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="prescription_releases_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "revoked_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="prescription_releases_revoked",
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
            ],
        ),
        migrations.CreateModel(
            name="VerificationProbe",
            fields=[
                (
                    "probe_key",
                    models.BinaryField(
                        max_length=32, primary_key=True, serialize=False
                    ),
                ),
                ("window_start", models.DateTimeField()),
                ("lookups", models.PositiveIntegerField()),
            ],
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocumentrevocation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    reason__in=[
                        "clinical_error",
                        "issuance_error",
                        "issuer_request",
                        "patient_request",
                    ]
                ),
                name="prescription_revocation_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocumentrelease",
            constraint=models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(revoked_at__isnull=True),
                name="prescription_release_active_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondocumentrelease",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(revoked_at__isnull=True, revoked_by__isnull=True)
                    | models.Q(revoked_at__isnull=False, revoked_by__isnull=False)
                ),
                name="prescription_release_revocation_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="verificationprobe",
            constraint=models.CheckConstraint(
                condition=models.Q(lookups__gte=0),
                name="prescription_probe_lookups_positive",
            ),
        ),
    ]
