"""Additive encrypted fiscal state, serialized without a database connection."""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

import apps.tenancy.fields

OPERATIONS = [
    migrations.CreateModel(
        name="FiscalConfiguration",
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
            ("legal_entity_reference", models.UUIDField()),
            (
                "legal_entity",
                apps.tenancy.fields.EncryptedJSONField(
                    purpose="billing.fiscal.legal_entity"
                ),
            ),
            ("provider", models.CharField(max_length=16)),
            ("municipality_code", models.CharField(max_length=7)),
            ("service_code", models.CharField(max_length=32)),
            ("tax_regime", models.CharField(max_length=64)),
            ("ibs_cbs_applicability", models.CharField(max_length=16)),
            (
                "tax_fields",
                apps.tenancy.fields.EncryptedJSONField(
                    purpose="billing.fiscal.configuration_taxes"
                ),
            ),
            ("review_reference", models.UUIDField(blank=True, null=True)),
            ("review_kind", models.CharField(max_length=24)),
            ("reviewed_at", models.DateTimeField(blank=True, null=True)),
            ("expires_at", models.DateTimeField(blank=True, null=True)),
            ("cancellation_window_seconds", models.PositiveIntegerField()),
            (
                "created_at",
                models.DateTimeField(default=django.utils.timezone.now, editable=False),
            ),
            (
                "book",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="billing.journalbook",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
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
    migrations.CreateModel(
        name="FiscalDocument",
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
            ("legal_entity_reference", models.UUIDField()),
            ("provider", models.CharField(max_length=16)),
            ("municipality_code", models.CharField(max_length=7)),
            ("service_code", models.CharField(max_length=32)),
            (
                "tax_fields",
                apps.tenancy.fields.EncryptedJSONField(
                    purpose="billing.fiscal.configuration_taxes"
                ),
            ),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("rehearsal", models.BooleanField()),
            (
                "status",
                models.CharField(
                    choices=[
                        ("draft", "Draft"),
                        ("submitted", "Submitted"),
                        ("authorized", "Authorized"),
                        ("rejected", "Rejected"),
                        ("cancelled", "Cancelled"),
                    ],
                    default="draft",
                    max_length=16,
                ),
            ),
            ("create_command_id", models.UUIDField()),
            ("create_fingerprint", models.BinaryField(max_length=32)),
            ("last_command_id", models.UUIDField()),
            (
                "dps_id",
                apps.tenancy.fields.EncryptedTextField(purpose="billing.fiscal.dps_id"),
            ),
            (
                "request_xml",
                apps.tenancy.fields.EncryptedBytesField(
                    purpose="billing.fiscal.request_xml"
                ),
            ),
            (
                "provider_reference",
                apps.tenancy.fields.EncryptedTextField(
                    blank=True, null=True, purpose="billing.fiscal.provider_reference"
                ),
            ),
            (
                "xml_payload",
                apps.tenancy.fields.EncryptedBytesField(
                    blank=True, null=True, purpose="billing.fiscal.xml"
                ),
            ),
            (
                "pdf_payload",
                apps.tenancy.fields.EncryptedBytesField(
                    blank=True, null=True, purpose="billing.fiscal.pdf"
                ),
            ),
            (
                "cancellation_payload",
                apps.tenancy.fields.EncryptedBytesField(
                    blank=True, null=True, purpose="billing.fiscal.cancellation"
                ),
            ),
            ("xml_reference", models.UUIDField(blank=True, null=True)),
            ("pdf_reference", models.UUIDField(blank=True, null=True)),
            ("rejection_code", models.CharField(blank=True, max_length=16)),
            ("submitted_at", models.DateTimeField(blank=True, null=True)),
            ("authorized_at", models.DateTimeField(blank=True, null=True)),
            ("cancel_until", models.DateTimeField(blank=True, null=True)),
            ("cancelled_at", models.DateTimeField(blank=True, null=True)),
            (
                "created_at",
                models.DateTimeField(default=django.utils.timezone.now, editable=False),
            ),
            (
                "book",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="billing.journalbook",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "configuration",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="billing.fiscalconfiguration",
                ),
            ),
            (
                "invoice",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="billing.invoice"
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
        name="FiscalTransition",
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
            ("command_id", models.UUIDField()),
            ("from_status", models.CharField(blank=True, max_length=16)),
            ("to_status", models.CharField(max_length=16)),
            ("actor_id", models.UUIDField()),
            (
                "created_at",
                models.DateTimeField(default=django.utils.timezone.now, editable=False),
            ),
            (
                "document",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="billing.fiscaldocument",
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
        model_name="fiscaldocument",
        constraint=models.UniqueConstraint(
            fields=("organization", "create_command_id"),
            name="billing_fiscal_create_command_uniq",
        ),
    ),
    migrations.AddConstraint(
        model_name="fiscaldocument",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("amount_minor__gt", 0),
                (
                    "status__in",
                    ["draft", "submitted", "authorized", "rejected", "cancelled"],
                ),
            ),
            name="billing_fiscal_document_terms",
        ),
    ),
    migrations.AddConstraint(
        model_name="fiscaltransition",
        constraint=models.UniqueConstraint(
            fields=("organization", "command_id"),
            name="billing_fiscal_transition_command_uniq",
        ),
    ),
]
