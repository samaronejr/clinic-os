"""Protected SNCR recordings and immutable receipts; rollback = restore."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.prescription.migrations._issuance_sql import SQL
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


class ProtectedSchema(SeparateDatabaseAndState):
    """Publish protected fields and their closed grants in one transaction."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """Keep historical signatures, ciphertexts and class guards unchanged."""

    atomic = False
    dependencies: ClassVar = [
        ("prescription", "0014_independent_signature_verification"),
    ]
    schema_operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="SncrNumberAllocation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        editable=False,
                        serialize=False,
                    ),
                ),
                ("category", models.CharField(max_length=40, choices=CLASSES)),
                ("request_digest", models.CharField(max_length=64)),
                (
                    "recording",
                    EncryptedJSONField(
                        purpose="prescription.sncrnumberallocation.recording"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        to="identity.organization",
                        on_delete=django.db.models.deletion.CASCADE,
                    ),
                ),
                (
                    "clinic",
                    models.ForeignKey(
                        to="identity.clinic",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "issuer",
                    models.ForeignKey(
                        to=settings.AUTH_USER_MODEL,
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "issuer", "category", "request_digest"),
                        name="prescription_sncr_recording_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(request_digest__regex=r"^[0-9a-f]{64}$"),
                        name="prescription_sncr_recording_digest",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(category__in=[c for c, _ in CLASSES])
                        & ~models.Q(
                            category__in=["simple", "synthetic_non_controlled"]
                        ),
                        name="prescription_sncr_recording_class",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="PrescriptionIssuance",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        editable=False,
                        serialize=False,
                    ),
                ),
                ("command_id", models.UUIDField()),
                (
                    "state",
                    models.CharField(max_length=24, default="rehearsal_complete"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        to="identity.organization",
                        on_delete=django.db.models.deletion.CASCADE,
                    ),
                ),
                (
                    "clinic",
                    models.ForeignKey(
                        to="identity.clinic",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "issuer",
                    models.ForeignKey(
                        to=settings.AUTH_USER_MODEL,
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "document",
                    models.OneToOneField(
                        to="prescription.prescriptiondocument",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                (
                    "signature",
                    models.OneToOneField(
                        to="prescription.signatureoperation",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "command_id"),
                        name="prescription_issuance_command",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(state="rehearsal_complete"),
                        name="prescription_issuance_synthetic_only",
                    ),
                ],
            },
        ),
        migrations.RunSQL(SQL),
    ]
    operations: ClassVar[list[Operation]] = [
        ProtectedSchema(
            database_operations=schema_operations,
            state_operations=[
                operation
                for operation in schema_operations
                if not isinstance(operation, migrations.RunSQL)
            ],
        ),
    ]
