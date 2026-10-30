"""Create the omnichannel inbox tables with their posture, guards and resolver."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.comms.migrations import _inbox_sql


class ProtectedSchema(SchemaStateBatch):
    """Publish the encrypted tables and their posture in one transaction.

    Bootstrap default privileges give the runtime role full DML on a new table
    the moment it exists, so tables and contract must commit together: a failed
    contract leaves no table, never an unprotected one.
    """

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """No plaintext pass or reverse path; rollback = restore.

    Reversing would drop every encrypted conversation, message and note.
    """

    atomic = False

    dependencies: ClassVar = [
        ("audit", "0005_clinic_metadata_v2"),
        ("comms", "0007_portal_channel"),
        ("identity", "0041_retention_authority"),
        ("intake", "0025_survivor_confirmations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    _schema_operations: ClassVar = [
        migrations.CreateModel(
            name="Conversation",
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
                ("purpose", models.CharField(max_length=16)),
                ("state", models.CharField(default="open", max_length=16)),
                ("contact_fingerprint", models.CharField(max_length=64)),
                (
                    "contact_address",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="comms.conversation.contact_address"
                    ),
                ),
                (
                    "contact_label",
                    apps.tenancy.fields.EncryptedTextField(
                        null=True, purpose="comms.conversation.contact_label"
                    ),
                ),
                ("last_inbound_at", models.DateTimeField(blank=True, null=True)),
                ("last_activity_at", models.DateTimeField()),
                ("needs_clinician", models.BooleanField(default=False)),
                (
                    "triage_category",
                    models.CharField(blank=True, default="", max_length=16),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assignee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                        blank=True,
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
            ],
        ),
        migrations.CreateModel(
            name="InternalNote",
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
                    "body",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="comms.internalnote.body"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="comms.conversation",
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
            name="Message",
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
                ("direction", models.CharField(max_length=3)),
                (
                    "body",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="comms.message.body"
                    ),
                ),
                ("command_id", models.UUIDField(blank=True, null=True)),
                ("event_id", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "template_parameters",
                    apps.tenancy.fields.EncryptedTextField(
                        null=True, purpose="comms.message.template_parameters"
                    ),
                ),
                ("supported", models.BooleanField(default=True)),
                (
                    "provider_state",
                    models.CharField(blank=True, default="", max_length=16),
                ),
                ("created_at", models.DateTimeField()),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="messages",
                        to="comms.conversation",
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
                    "sent_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProviderAccount",
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
                ("provider", models.CharField(max_length=32)),
                ("external_account_id", models.CharField(max_length=128)),
                ("signing_key_digest", models.CharField(max_length=64)),
                (
                    "verify_token_digest",
                    models.CharField(blank=True, default="", max_length=64),
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "principal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.serviceprincipal",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="InboundEvent",
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
                ("event_id", models.CharField(max_length=255)),
                ("kind", models.CharField(max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "provider_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="comms.provideraccount",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="conversation",
            name="provider_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="comms.provideraccount",
            ),
        ),
        migrations.CreateModel(
            name="QuickReply",
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
                ("title", models.CharField(max_length=80)),
                ("body", models.TextField(max_length=1024)),
                ("purpose", models.CharField(max_length=16)),
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
            ],
        ),
        migrations.CreateModel(
            name="SuggestedReply",
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
                    "body",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="comms.suggestedreply.body"
                    ),
                ),
                ("state", models.CharField(default="proposed", max_length=16)),
                ("drafter", models.CharField(max_length=32)),
                ("invocation_ref", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="comms.conversation",
                    ),
                ),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "in_reply_to",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="suggestions",
                        to="comms.message",
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
                    "used_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="comms.message",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TemplateVersion",
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
                ("name", models.CharField(max_length=64)),
                ("language", models.CharField(default="pt_BR", max_length=16)),
                ("version", models.PositiveIntegerField()),
                ("category", models.CharField(max_length=16)),
                ("purpose", models.CharField(max_length=16)),
                ("body", models.TextField(max_length=1024)),
                ("parameter_count", models.PositiveSmallIntegerField(default=0)),
                ("content_digest", models.CharField(max_length=64)),
                ("approval_status", models.CharField(default="draft", max_length=16)),
                ("approval_changed_at", models.DateTimeField(blank=True, null=True)),
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
            ],
        ),
        migrations.AddField(
            model_name="message",
            name="template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="comms.templateversion",
            ),
        ),
        migrations.CreateModel(
            name="MessageMeter",
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
                ("source", models.CharField(max_length=16)),
                ("category", models.CharField(max_length=32)),
                ("billable", models.BooleanField(null=True)),
                (
                    "pricing_type",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.ForeignKey(
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
                        fields=("message", "source"),
                        name="comms_meter_message_source_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("source__in", ("expected", "provider"))),
                        name="comms_meter_source",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Participant",
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
                ("role", models.CharField(max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="comms.conversation",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        blank=True,
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
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("role__in", ("watcher", "patient")),
                            models.Q(
                                models.Q(
                                    ("enrollment__isnull", True),
                                    ("role", "watcher"),
                                    ("user__isnull", False),
                                ),
                                models.Q(
                                    ("enrollment__isnull", False),
                                    ("role", "patient"),
                                    ("user__isnull", True),
                                ),
                                _connector="OR",
                            ),
                        ),
                        name="comms_participant_contract",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("user__isnull", False)),
                        fields=("conversation", "user"),
                        name="comms_participant_user_uniq",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("enrollment__isnull", False)),
                        fields=("conversation", "enrollment"),
                        name="comms_participant_enrollment_uniq",
                    ),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.UniqueConstraint(
                fields=("provider", "external_account_id"),
                name="comms_account_provider_external_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="comms_account_org_clinic_id_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="provideraccount",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("channel__in", ["whatsapp", "email", "sms"]),
                    (
                        "provider__in",
                        ["whatsapp-cloud-v1", "inbox-email-v1", "inbox-sms-v1"],
                    ),
                    models.Q(
                        models.Q(
                            ("channel", "whatsapp"), ("provider", "whatsapp-cloud-v1")
                        ),
                        models.Q(("channel", "email"), ("provider", "inbox-email-v1")),
                        models.Q(("channel", "sms"), ("provider", "inbox-sms-v1")),
                        _connector="OR",
                    ),
                    ("signing_key_digest__regex", "^[0-9a-f]{64}$"),
                ),
                name="comms_account_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundevent",
            constraint=models.UniqueConstraint(
                fields=("provider_account", "event_id"), name="comms_inbound_event_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="inboundevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ("message", "status", "template"))),
                name="comms_inbound_event_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "closed"), _negated=True),
                fields=("clinic", "channel", "contact_fingerprint", "purpose"),
                name="comms_conversation_live_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="comms_conversation_org_clinic_id_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("channel__in", ["whatsapp", "email", "sms", "portal"]),
                    ("purpose__in", ["transactional", "care", "marketing"]),
                    ("state__in", ["open", "pending", "closed"]),
                    ("contact_fingerprint__regex", "^[0-9a-f]{64}$"),
                    models.Q(
                        models.Q(
                            ("channel", "portal"), ("provider_account__isnull", True)
                        ),
                        models.Q(
                            ("channel__in", ["whatsapp", "email", "sms"]),
                            ("provider_account__isnull", False),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("purpose", "care"), _negated=True),
                        ("enrollment__isnull", False),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("channel", "portal"), _negated=True),
                        ("enrollment__isnull", False),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("purpose", "marketing"), _negated=True),
                        models.Q(("channel", "portal"), _negated=True),
                        _connector="OR",
                    ),
                ),
                name="comms_conversation_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="quickreply",
            constraint=models.CheckConstraint(
                condition=models.Q(("purpose__in", ("transactional", "care"))),
                name="comms_quickreply_purpose",
            ),
        ),
        migrations.AddConstraint(
            model_name="suggestedreply",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("state__in", ["proposed", "used", "edited", "discarded"])
                ),
                name="comms_suggestion_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="suggestedreply",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "proposed")),
                fields=("in_reply_to",),
                name="comms_suggestion_live_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="templateversion",
            constraint=models.UniqueConstraint(
                fields=("clinic", "channel", "name", "language", "version"),
                name="comms_template_version_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="templateversion",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="comms_template_org_clinic_id_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="templateversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("channel__in", ["whatsapp", "email", "sms"]),
                    ("category__in", ["utility", "marketing", "authentication"]),
                    ("purpose__in", ["transactional", "care", "marketing"]),
                    (
                        "approval_status__in",
                        [
                            "draft",
                            "pending",
                            "approved",
                            "rejected",
                            "paused",
                            "disabled",
                        ],
                    ),
                    ("name__regex", "^[a-z0-9_]{1,64}$"),
                    ("version__gte", 1),
                    ("content_digest__regex", "^[0-9a-f]{64}$"),
                    models.Q(
                        models.Q(("category", "marketing"), ("purpose", "marketing")),
                        models.Q(
                            ("category", "utility"),
                            ("purpose__in", ("transactional", "care")),
                        ),
                        models.Q(
                            ("category", "authentication"), ("purpose", "transactional")
                        ),
                        _connector="OR",
                    ),
                ),
                name="comms_template_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.UniqueConstraint(
                condition=models.Q(("command_id__isnull", False)),
                fields=("organization", "command_id"),
                name="comms_message_command_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.UniqueConstraint(
                condition=models.Q(("event_id__isnull", False)),
                fields=("conversation", "event_id"),
                name="comms_message_event_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(
                            ("command_id__isnull", False),
                            ("direction", "out"),
                            ("event_id__isnull", True),
                            ("sent_by__isnull", False),
                        ),
                        models.Q(
                            ("command_id__isnull", True),
                            ("direction", "in"),
                            ("event_id__isnull", False),
                            ("sent_by__isnull", True),
                            ("template__isnull", True),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        ("provider_state", ""),
                        ("provider_state__in", ["sent", "delivered", "read", "failed"]),
                        _connector="OR",
                    ),
                ),
                name="comms_message_contract",
            ),
        ),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[
                *_schema_operations,
                migrations.RunSQL(_inbox_sql.SQL),
            ],
            state_operations=_schema_operations,
        ),
    ]
