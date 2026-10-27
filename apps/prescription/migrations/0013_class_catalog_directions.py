"""Add manual classes and protected directions; irreversible, rollback = restore."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.prescription.migrations._class_catalog_sql import SQL
from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.migration_operations import SeparateDatabaseAndState

CLASSES = [
    ("simple", "Simple prescription"),
    ("controle_especial", "Special-control prescription"),
    ("antimicrobial_retention", "Antimicrobial retention"),
    ("glp1_retention", "GLP-1 retention"),
    ("notificacao_a", "Notification A"),
    ("notificacao_b", "Notification B"),
    ("notificacao_b2", "Notification B2"),
    ("retinoid_systemic", "Systemic retinoid"),
    ("thalidomide", "Thalidomide"),
    ("synthetic_non_controlled", "Synthetic rehearsal; no real issuance"),
]
REAL_CLASSES = [
    category for category, _ in CLASSES if category != "synthetic_non_controlled"
]
SEED_SQL = """
INSERT INTO clinic_app.prescription_prescriptionclasspolicy
 (category,electronic_allowed_from,requires_sncr,signature_level,paper_only_until_gate)
VALUES
 ('simple',NULL,false,'qualified',true),
 ('controle_especial','2026-10-30',true,'qualified',true),
 ('antimicrobial_retention','2026-10-30',true,'qualified',true),
 ('glp1_retention','2026-10-30',true,'qualified',true),
 ('notificacao_a','2026-09-30',true,'qualified',true),
 ('notificacao_b','2026-09-30',true,'qualified',true),
 ('notificacao_b2','2026-09-30',true,'qualified',true),
 ('retinoid_systemic','2026-09-30',true,'qualified',true),
 ('thalidomide','2026-09-30',true,'qualified',true),
 ('synthetic_non_controlled',NULL,false,'synthetic',false);
"""


class ProtectedSchema(SeparateDatabaseAndState):
    """Publish encrypted fields, grants and local-role SQL in one transaction."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """Install encrypted columns directly and preserve every synthetic artifact."""

    atomic = False
    dependencies: ClassVar = [
        ("prescription", "0012_clinical_document_kinds"),
        ("ehr", "0014_longitudinal_record"),
    ]
    schema_operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PrescriptionClassPolicy",
            fields=[
                (
                    "category",
                    models.CharField(
                        choices=CLASSES,
                        max_length=40,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("electronic_allowed_from", models.DateField(null=True)),
                ("requires_sncr", models.BooleanField()),
                ("signature_level", models.CharField(max_length=16)),
                ("paper_only_until_gate", models.BooleanField()),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(category__in=[c for c, _ in CLASSES]),
                        name="prescription_policy_class",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            signature_level__in=["qualified", "synthetic"]
                        ),
                        name="prescription_policy_signature",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="MedicationCatalogImport",
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
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("source_url", models.URLField(max_length=1000)),
                ("retrieved_on", models.DateField()),
                ("checksum", models.CharField(max_length=64, unique=True)),
                ("loaded_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            schema_version=1, checksum__regex=r"^[0-9a-f]{64}$"
                        ),
                        name="prescription_catalog_source_shape",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="MedicationCatalogEntry",
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
                ("code", models.CharField(max_length=80)),
                ("description", models.CharField(max_length=240)),
                ("strength_form", models.CharField(max_length=160)),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="prescription.medicationcatalogimport",
                    ),
                ),
            ],
            options={
                "ordering": ["description", "code", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("source", "code"),
                        name="prescription_catalog_source_code",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="MedicationFavorite",
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
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
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
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="prescription.medicationcatalogentry",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "issuer", "entry"),
                        name="prescription_favorite_issuer_entry",
                    ),
                ]
            },
        ),
        migrations.RemoveConstraint(
            model_name="prescriptiondraft", name="prescription_synthetic_contract"
        ),
        migrations.AlterField(
            model_name="prescriptiondraft",
            name="category",
            field=models.CharField(
                max_length=40, choices=CLASSES, null=True, blank=True
            ),
        ),
        migrations.AddField(
            model_name="prescriptiondraft",
            name="renewal_source",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="prescription.prescriptiondocument",
            ),
        ),
        migrations.AddConstraint(
            model_name="prescriptiondraft",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    category__isnull=False,
                    category="synthetic_non_controlled",
                    contract_version="synthetic-draft-v1",
                )
                | models.Q(
                    category__isnull=True, contract_version="prescription-classes-v1"
                )
                | models.Q(
                    category__isnull=False,
                    category__in=REAL_CLASSES,
                    contract_version="prescription-classes-v1",
                ),
                name="prescription_class_contract",
            ),
        ),
        migrations.AddField(
            model_name="prescriptionitem",
            name="directions",
            field=EncryptedJSONField(
                purpose="prescription.prescriptionitem.directions", null=True
            ),
        ),
        migrations.AddField(
            model_name="prescriptionitem",
            name="catalog_entry",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="prescription.medicationcatalogentry",
            ),
        ),
        migrations.AddField(
            model_name="prescriptionitem",
            name="medication_statement",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="ehr.medicationstatement",
            ),
        ),
        migrations.AddField(
            model_name="signatureoperation",
            name="category",
            field=models.CharField(
                max_length=40, choices=CLASSES, null=True, blank=True
            ),
        ),
        migrations.RunSQL(SEED_SQL),
        migrations.RunSQL(SQL),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=~models.Q(state="issued")
                | models.Q(category__isnull=False, category__in=REAL_CLASSES),
                name="prescription_issued_class_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="signatureoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(category__isnull=True)
                | models.Q(category__in=[c for c, _ in CLASSES]),
                name="prescription_signature_class",
            ),
        ),
    ]
    operations: ClassVar[list[Operation]] = [
        ProtectedSchema(
            database_operations=schema_operations,
            state_operations=[
                operation
                for operation in schema_operations
                if not isinstance(operation, migrations.RunSQL)
            ],
        )
    ]
