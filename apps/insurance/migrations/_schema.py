"""Frozen encrypted insurance model state; first-write envelopes only."""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields

MODELS = [
    migrations.CreateModel(
        name="Batch",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("number", models.CharField(max_length=12)),
            ("state", models.CharField(default="draft", max_length=16)),
            ("guide_ids", models.JSONField()),
            ("versions", models.JSONField()),
            ("xml", apps.tenancy.fields.EncryptedTextField(purpose="insurance.batch")),
            ("content_sha256", models.CharField(max_length=64)),
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
        name="Glosa",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("sequence", models.PositiveIntegerField()),
            ("charged_minor", models.PositiveBigIntegerField()),
            ("accepted_minor", models.PositiveBigIntegerField()),
            ("denied_minor", models.PositiveBigIntegerField()),
            ("code", models.CharField(blank=True, max_length=4)),
            (
                "detail",
                apps.tenancy.fields.EncryptedJSONField(
                    empty=dict, purpose="insurance.adjudication"
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
        name="Appeal",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("state", models.CharField(default="draft", max_length=16)),
            (
                "body",
                apps.tenancy.fields.EncryptedTextField(purpose="insurance.appeal"),
            ),
            (
                "clinical_assertions",
                apps.tenancy.fields.EncryptedTextField(
                    purpose="insurance.appeal_assertions", null=True
                ),
            ),
            ("content_sha256", models.CharField(max_length=64)),
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
            (
                "glosa",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.glosa"
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.CreateModel(
        name="Guide",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("kind", models.CharField(max_length=16)),
            ("number", models.CharField(max_length=20)),
            ("state", models.CharField(default="draft", max_length=16)),
            (
                "facts",
                apps.tenancy.fields.EncryptedJSONField(
                    empty=dict, purpose="insurance.guide"
                ),
            ),
            ("total_minor", models.PositiveBigIntegerField()),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "encounter",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
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
            (
                "predecessor",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="successor",
                    to="insurance.guide",
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.AddField(
        model_name="glosa",
        name="guide",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, to="insurance.guide"
        ),
    ),
    migrations.CreateModel(
        name="BatchGuide",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "batch",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.batch"
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
            (
                "guide",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.guide"
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.CreateModel(
        name="InsuranceMembership",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "details",
                apps.tenancy.fields.EncryptedJSONField(
                    empty=dict, purpose="insurance.membership"
                ),
            ),
            (
                "valid_from",
                apps.tenancy.fields.EncryptedDateField(
                    purpose="insurance.membership.valid_from"
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "enrollment",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="intake.patientclinicenrollment",
                ),
            ),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="insurance_memberships",
                    to="identity.organization",
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.AddField(
        model_name="guide",
        name="membership",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT,
            to="insurance.insurancemembership",
        ),
    ),
    migrations.CreateModel(
        name="Payer",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("ans_registry", models.CharField(max_length=6)),
            ("name", models.CharField(max_length=160)),
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
    migrations.AddField(
        model_name="insurancemembership",
        name="payer",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT,
            related_name="claim_memberships",
            to="insurance.payer",
        ),
    ),
    migrations.CreateModel(
        name="PayerContract",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("version", models.PositiveIntegerField()),
            ("starts_on", models.DateField()),
            ("ends_on", models.DateField(null=True)),
            (
                "billing_identity",
                apps.tenancy.fields.EncryptedJSONField(
                    empty=dict, purpose="insurance.contract"
                ),
            ),
            ("webservice", models.BooleanField(default=False)),
            ("eligibility_supported", models.BooleanField(default=False)),
            ("authorization_supported", models.BooleanField(default=False)),
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
            (
                "payer",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.payer"
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.AddField(
        model_name="guide",
        name="contract",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, to="insurance.payercontract"
        ),
    ),
    migrations.AddField(
        model_name="batch",
        name="contract",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, to="insurance.payercontract"
        ),
    ),
    migrations.CreateModel(
        name="PayerRequest",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("kind", models.CharField(max_length=16)),
            ("outcome", models.CharField(default="unknown", max_length=16)),
            (
                "evidence",
                apps.tenancy.fields.EncryptedJSONField(
                    empty=dict, purpose="insurance.request"
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "contract",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="insurance.payercontract",
                ),
            ),
            (
                "membership",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="insurance.insurancemembership",
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
                "task",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="workflows.task"
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.CreateModel(
        name="PaymentMatch",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("amount_minor", models.PositiveBigIntegerField()),
            (
                "allocation",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="billing.settlementallocation",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "guide",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.guide"
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
                "settlement",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="billing.journalsettlement",
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.CreateModel(
        name="PriceTable",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("version", models.PositiveIntegerField()),
            (
                "prices",
                apps.tenancy.fields.EncryptedJSONField(
                    empty=dict, purpose="insurance.prices"
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "contract",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="insurance.payercontract",
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
    migrations.AddField(
        model_name="guide",
        name="price_table",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, to="insurance.pricetable"
        ),
    ),
    migrations.CreateModel(
        name="Return",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("source_sha256", models.CharField(max_length=64)),
            (
                "xml",
                apps.tenancy.fields.EncryptedBytesField(purpose="insurance.return"),
            ),
            (
                "batch",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.batch"
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
    migrations.AddField(
        model_name="glosa",
        name="source",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, to="insurance.return"
        ),
    ),
    migrations.CreateModel(
        name="Submission",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("channel", models.CharField(max_length=24)),
            ("content_sha256", models.CharField(max_length=64)),
            (
                "batch",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.batch"
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
        name="TussTerm",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("component_version", models.CharField(max_length=16)),
            ("source_date", models.DateField()),
            ("source_sha256", models.CharField(max_length=64)),
            ("table_code", models.CharField(max_length=2)),
            ("code", models.CharField(max_length=10)),
            ("description", models.CharField(max_length=500)),
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
        name="ClinicalApproval",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("content_sha256", models.CharField(max_length=64)),
            (
                "appeal",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT, to="insurance.appeal"
                ),
            ),
            (
                "approved_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to=settings.AUTH_USER_MODEL,
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
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"),
                    name="insurance_clinicalapproval_cmd",
                )
            ],
        },
    ),
    migrations.CreateModel(
        name="CommandReceipt",
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
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("subject_id", models.UUIDField()),
            ("kind", models.CharField(max_length=32)),
            ("next_state", models.CharField(max_length=16)),
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
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"), name="insurance_commandreceipt_cmd"
                )
            ],
        },
    ),
    migrations.AddConstraint(
        model_name="appeal",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_appeal_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="batchguide",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_batchguide_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="payer",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_payer_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="insurancemembership",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_insurancemembership_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="payercontract",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_payercontract_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="batch",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_batch_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="payerrequest",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_payerrequest_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="paymentmatch",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_paymentmatch_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="pricetable",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_pricetable_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="guide",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_guide_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="return",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_return_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="return",
        constraint=models.UniqueConstraint(
            fields=("batch", "source_sha256"), name="insurance_return_file"
        ),
    ),
    migrations.AddConstraint(
        model_name="glosa",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_glosa_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="submission",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_submission_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="tussterm",
        constraint=models.UniqueConstraint(
            fields=("clinic", "command_id"), name="insurance_tussterm_cmd"
        ),
    ),
    migrations.AddConstraint(
        model_name="tussterm",
        constraint=models.UniqueConstraint(
            fields=("clinic", "component_version", "table_code", "code"),
            name="insurance_tuss_version_code",
        ),
    ),
    migrations.AddField(
        model_name="insurancemembership",
        name="registration",
        field=models.ForeignKey(
            to="intake.insurancemembership",
            on_delete=models.PROTECT,
            related_name="claim_memberships",
        ),
    ),
    migrations.AddField(
        model_name="payerrequest",
        name="appointment",
        field=models.ForeignKey(
            to="scheduling.appointment",
            on_delete=models.PROTECT,
            related_name="payer_checks",
        ),
    ),
    migrations.AddField(
        model_name="payerrequest",
        name="authorization_reference",
        field=models.CharField(max_length=64, blank=True, default=""),
    ),
]
