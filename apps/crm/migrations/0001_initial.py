"""Add encrypted CRM tables and exact FORCE RLS without a plaintext cutover."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.crm.migrations._crm_sql import REVERSE, SQL

# Each new or changed model renders once instead of re-rendering the tenant graph.
_BATCH_1 = [
    migrations.CreateModel(
        name="Campaign",
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
                "title",
                apps.tenancy.fields.EncryptedTextField(purpose="crm.campaign.title"),
            ),
            (
                "state",
                models.CharField(
                    choices=[
                        ("draft", "Draft"),
                        ("approved", "Approved"),
                        ("sending", "Sending"),
                        ("sent", "Sent"),
                    ],
                    default="draft",
                    max_length=16,
                ),
            ),
            ("revision", models.PositiveIntegerField(default=1)),
            ("excluded_count", models.PositiveIntegerField(default=0)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
                ),
            ),
            (
                "created_by",
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
                "template",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="comms.templateversion",
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.CreateModel(
        name="PublicBookingProfile",
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
            ("code", models.CharField(max_length=40, unique=True)),
            ("description", models.TextField(max_length=400)),
            ("active", models.BooleanField(default=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "clinic",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
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
        name="ReferralSource",
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
            ("code", models.CharField(max_length=40)),
            (
                "name",
                apps.tenancy.fields.EncryptedTextField(
                    purpose="crm.referralsource.name"
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
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="Lead",
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
                "name",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="crm.lead.name"
                ),
            ),
            (
                "email",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="crm.lead.email"
                ),
            ),
            ("state", models.CharField(default="active", max_length=16)),
            ("expires_at", models.DateTimeField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
                ),
            ),
            (
                "enrollment",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="intake.patientclinicenrollment",
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
                "source",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="crm.referralsource",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="SatisfactionSurvey",
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
            ("score", models.PositiveSmallIntegerField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "appointment",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="scheduling.appointment",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
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
                "patient_session",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="intake.patientsession",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="CampaignSend",
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
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "campaign",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="crm.campaign"
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
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
                "message",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT, to="comms.message"
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
            "constraints": [
                models.UniqueConstraint(
                    fields=("campaign", "enrollment"), name="crm_send_recipient"
                )
            ],
        },
    ),
    migrations.CreateModel(
        name="MarketingPreference",
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
            ("channel", models.CharField(max_length=16)),
            ("opted_in", models.BooleanField()),
            ("contact_version", models.PositiveIntegerField(default=0)),
            ("revision", models.PositiveIntegerField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
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
                    to="identity.organization",
                ),
            ),
            (
                "patient_session",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="intake.patientsession",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("enrollment", "channel", "revision"),
                    name="crm_marketing_revision",
                ),
                models.CheckConstraint(
                    condition=models.Q(("channel__in", ("email", "sms", "whatsapp"))),
                    name="crm_marketing_channel",
                ),
            ],
        },
    ),
    migrations.AddConstraint(
        model_name="referralsource",
        constraint=models.UniqueConstraint(
            fields=("clinic", "code"), name="crm_source_code"
        ),
    ),
    migrations.AddConstraint(
        model_name="lead",
        constraint=models.CheckConstraint(
            condition=models.Q(
                models.Q(("enrollment__isnull", True), ("state", "active")),
                models.Q(("enrollment__isnull", False), ("state", "converted")),
                models.Q(
                    ("email__isnull", True),
                    ("enrollment__isnull", True),
                    ("name__isnull", True),
                    ("state", "redacted"),
                ),
                _connector="OR",
            ),
            name="crm_lead_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="satisfactionsurvey",
        constraint=models.CheckConstraint(
            condition=models.Q(("score__lte", 10)), name="crm_score"
        ),
    ),
]


class Migration(migrations.Migration):
    """Install the additive CRM slice; populated rollback requires restore."""

    initial = True

    dependencies: ClassVar = [
        ("comms", "0011_agent_replies"),
        ("consent", "0005_delegate_purposes"),
        ("identity", "0043_delegate_messages_operation"),
        ("intake", "0028_messages_operation"),
        ("scheduling", "0010_patient_service_booking"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        SchemaStateBatch(database_operations=_BATCH_1, state_operations=_BATCH_1),
        migrations.RunSQL(SQL, REVERSE),
    ]
