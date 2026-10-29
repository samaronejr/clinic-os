"""Add encrypted clinical-document snapshots; irreversible, rollback = restore."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.prescription.migrations._clinical_documents_sql import SQL
from apps.tenancy.fields import EncryptedJSONField


class Migration(migrations.Migration):
    """Install protected fields directly, with no plaintext intermediate column."""

    atomic = False
    dependencies: ClassVar = [
        ("prescription", "0011_delegate_confidentiality"),
        ("orders", "0004_matched_patient_authority"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="prescriptiondraft",
            name="kind",
            field=models.CharField(
                choices=[
                    ("prescription", "Prescription"),
                    ("certificate", "Medical certificate"),
                    ("attestation", "Attendance attestation"),
                    ("referral", "Referral"),
                    ("exam_request", "Exam request"),
                    ("instruction", "Patient instructions"),
                ],
                default="prescription",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="prescriptiondraft",
            name="encounter",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondraft",
            constraint=models.UniqueConstraint(
                fields=("encounter", "kind"), name="prescription_encounter_kind"
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondraft",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        "prescription",
                        "certificate",
                        "attestation",
                        "referral",
                        "exam_request",
                        "instruction",
                    ]
                ),
                name="prescription_document_kind",
            ),
        ),
        migrations.CreateModel(
            name="DocumentDraftContent",
            fields=[
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
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
                (
                    "content",
                    EncryptedJSONField(
                        purpose="prescription.documentdraftcontent.content"
                    ),
                ),
                (
                    "draft",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="prescription.prescriptiondraft",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("draft", "version"), name="prescription_content_version"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(version__gte=2),
                        name="prescription_content_version_positive",
                    ),
                ],
            },
        ),
        migrations.RunSQL(SQL),
    ]
