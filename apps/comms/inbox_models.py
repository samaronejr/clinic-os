"""Tenant-scoped omnichannel inbox records.

Message bodies, contact addresses and profile labels are envelope-encrypted
fields; the outbox keeps only routing metadata. A conversation's channel,
contact and clinic never change, its purpose changes at most once (an
unverified transactional thread becomes a care thread when the patient link is
verified), and a message is immutable except for the provider's delivery state.
"""

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.db import models
from django.db.models.constraints import BaseConstraint

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel

CHANNEL_VALUES: Final = ["whatsapp", "email", "sms", "portal"]
EXTERNAL_CHANNEL_VALUES: Final = ["whatsapp", "email", "sms"]
PURPOSE_VALUES: Final = ["transactional", "care", "marketing"]
STATE_VALUES: Final = ["open", "pending", "closed"]
PROVIDER_VALUES: Final = ["whatsapp-cloud-v1", "inbox-email-v1", "inbox-sms-v1"]
CATEGORY_VALUES: Final = ["utility", "marketing", "authentication"]
APPROVAL_VALUES: Final = [
    "draft",
    "pending",
    "approved",
    "rejected",
    "paused",
    "disabled",
]
PROVIDER_STATE_VALUES: Final = ["sent", "delivered", "read", "failed"]
SUGGESTION_STATE_VALUES: Final = ["proposed", "used", "edited", "discarded"]
HEX64: Final = r"^[0-9a-f]{64}$"


class ProviderAccount(TenantScopedModel):
    """Owner-provisioned inbound identity; routing is by this row, never a payload."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    channel = models.CharField(max_length=16)
    provider = models.CharField(max_length=32)
    # Provider-side identifier (for WhatsApp the phone number id); a webhook
    # claiming any other value is ignored, so a payload cannot choose a clinic.
    external_account_id = models.CharField(max_length=128)
    principal = models.ForeignKey("identity.ServicePrincipal", on_delete=models.PROTECT)
    signing_key_digest = models.CharField(max_length=64)
    verify_token_digest = models.CharField(max_length=64, blank=True, default="")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One provider identifier belongs to exactly one clinic account."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("provider", "external_account_id"),
                name="comms_account_provider_external_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="comms_account_org_clinic_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=EXTERNAL_CHANNEL_VALUES)
                & models.Q(provider__in=PROVIDER_VALUES)
                & (
                    models.Q(channel="whatsapp", provider="whatsapp-cloud-v1")
                    | models.Q(channel="email", provider="inbox-email-v1")
                    | models.Q(channel="sms", provider="inbox-sms-v1")
                )
                & models.Q(signing_key_digest__regex=HEX64),
                name="comms_account_contract",
            ),
        ]


class TemplateVersion(TenantScopedModel):
    """One immutable template body per channel; only provider approval changes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    channel = models.CharField(max_length=16)
    name = models.CharField(max_length=64)
    language = models.CharField(max_length=16, default="pt_BR")
    version = models.PositiveIntegerField()
    category = models.CharField(max_length=16)
    purpose = models.CharField(max_length=16)
    body = models.TextField(max_length=1024)
    parameter_count = models.PositiveSmallIntegerField(default=0)
    content_digest = models.CharField(max_length=64)
    approval_status = models.CharField(max_length=16, default="draft")
    approval_changed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Version numbers are per clinic, channel, name and language."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "channel", "name", "language", "version"),
                name="comms_template_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="comms_template_org_clinic_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=EXTERNAL_CHANNEL_VALUES)
                & models.Q(category__in=CATEGORY_VALUES)
                & models.Q(purpose__in=PURPOSE_VALUES)
                & models.Q(approval_status__in=APPROVAL_VALUES)
                & models.Q(name__regex=r"^[a-z0-9_]{1,64}$")
                & models.Q(version__gte=1)
                & models.Q(content_digest__regex=HEX64)
                & (
                    models.Q(category="marketing", purpose="marketing")
                    | models.Q(
                        category="utility", purpose__in=("transactional", "care")
                    )
                    | models.Q(category="authentication", purpose="transactional")
                ),
                name="comms_template_contract",
            ),
        ]


class QuickReply(TenantScopedModel):
    """A staff-authored snippet; it is text for a human to review, not a template."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    title = models.CharField(max_length=80)
    body = models.TextField(max_length=1024)
    purpose = models.CharField(max_length=16)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Marketing snippets cannot exist as quick replies."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(purpose__in=("transactional", "care")),
                name="comms_quickreply_purpose",
            ),
        ]


class Conversation(TenantScopedModel):
    """One thread with one contact on one channel and (almost) one purpose."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    provider_account = models.ForeignKey(
        ProviderAccount, null=True, blank=True, on_delete=models.PROTECT
    )
    channel = models.CharField(max_length=16)
    purpose = models.CharField(max_length=16)
    state = models.CharField(max_length=16, default="open")
    # NULL until staff verify that the contact belongs to this enrollment.
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    contact_fingerprint = models.CharField(max_length=64)
    contact_address = EncryptedTextField(purpose="comms.conversation.contact_address")
    contact_label = EncryptedTextField(
        purpose="comms.conversation.contact_label", null=True
    )
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField()
    needs_clinician = models.BooleanField(default=False)
    triage_category = models.CharField(max_length=16, blank=True, default="")
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """One live thread per contact, channel and purpose."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "channel", "contact_fingerprint", "purpose"),
                condition=~models.Q(state="closed"),
                name="comms_conversation_live_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="comms_conversation_org_clinic_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=CHANNEL_VALUES)
                & models.Q(purpose__in=PURPOSE_VALUES)
                & models.Q(state__in=STATE_VALUES)
                & models.Q(contact_fingerprint__regex=HEX64)
                & (
                    models.Q(channel="portal", provider_account__isnull=True)
                    | models.Q(
                        channel__in=EXTERNAL_CHANNEL_VALUES,
                        provider_account__isnull=False,
                    )
                )
                & (~models.Q(purpose="care") | models.Q(enrollment__isnull=False))
                & (~models.Q(channel="portal") | models.Q(enrollment__isnull=False))
                & (~models.Q(purpose="marketing") | ~models.Q(channel="portal")),
                name="comms_conversation_contract",
            ),
        ]


class Message(TenantScopedModel):
    """An immutable message; the provider's delivery state is the only mutation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.PROTECT, related_name="messages"
    )
    direction = models.CharField(max_length=3)
    body = EncryptedTextField(purpose="comms.message.body")
    template = models.ForeignKey(
        TemplateVersion, null=True, blank=True, on_delete=models.PROTECT
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    # Outbound idempotency key and its operation subject; inbound provider id.
    command_id = models.UUIDField(null=True, blank=True)
    event_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    # JSON list of the positional template parameters, kept to rebuild the send.
    template_parameters = EncryptedTextField(
        purpose="comms.message.template_parameters", null=True
    )
    supported = models.BooleanField(default=True)
    provider_state = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField()

    class Meta:
        """Pin direction contracts and replay-safe uniqueness."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "command_id"),
                condition=models.Q(command_id__isnull=False),
                name="comms_message_command_uniq",
            ),
            models.UniqueConstraint(
                fields=("conversation", "event_id"),
                condition=models.Q(event_id__isnull=False),
                name="comms_message_event_uniq",
            ),
            models.CheckConstraint(
                # An outbound row without a sender is the front-desk agent's
                # reply (todo 54); the message guard admits it only from the
                # agent's finish bridge, with the turn id as its command id.
                condition=(
                    models.Q(
                        direction="out",
                        command_id__isnull=False,
                        event_id__isnull=True,
                    )
                    | models.Q(
                        direction="in",
                        sent_by__isnull=True,
                        command_id__isnull=True,
                        event_id__isnull=False,
                        template__isnull=True,
                    )
                )
                & (
                    models.Q(provider_state="")
                    | models.Q(provider_state__in=PROVIDER_STATE_VALUES)
                ),
                name="comms_message_contract",
            ),
        ]


class Participant(TenantScopedModel):
    """Who takes part in a conversation: staff watchers or the verified patient."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    role = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Exactly one identity per participant row."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(role__in=("watcher", "patient"))
                & (
                    models.Q(
                        role="watcher", user__isnull=False, enrollment__isnull=True
                    )
                    | models.Q(
                        role="patient", user__isnull=True, enrollment__isnull=False
                    )
                ),
                name="comms_participant_contract",
            ),
            models.UniqueConstraint(
                fields=("conversation", "user"),
                condition=models.Q(user__isnull=False),
                name="comms_participant_user_uniq",
            ),
            models.UniqueConstraint(
                fields=("conversation", "enrollment"),
                condition=models.Q(enrollment__isnull=False),
                name="comms_participant_enrollment_uniq",
            ),
        ]


class InternalNote(TenantScopedModel):
    """Staff-only note; it is never part of anything sent to the contact."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    body = EncryptedTextField(purpose="comms.internalnote.body")
    created_at = models.DateTimeField(auto_now_add=True)


class InboundEvent(TenantScopedModel):
    """Replay receipt: one row per provider event id, no payload."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=255)
    kind = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A replayed delivery hits this constraint and is acknowledged once."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("provider_account", "event_id"),
                name="comms_inbound_event_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=("message", "status", "template")),
                name="comms_inbound_event_kind",
            ),
        ]


class MessageMeter(TenantScopedModel):
    """Billing facts: what we expected to send and what the provider charged for."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.PROTECT)
    source = models.CharField(max_length=16)
    category = models.CharField(max_length=32)
    billable = models.BooleanField(null=True)
    pricing_type = models.CharField(max_length=32, blank=True, default="")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One expected and one provider-reported row per message."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("message", "source"), name="comms_meter_message_source_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=("expected", "provider")),
                name="comms_meter_source",
            ),
        ]


class SuggestedReply(TenantScopedModel):
    """An AI draft shown for review; nothing here is ever sent automatically."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT)
    in_reply_to = models.ForeignKey(
        Message, on_delete=models.PROTECT, related_name="suggestions"
    )
    body = EncryptedTextField(purpose="comms.suggestedreply.body")
    state = models.CharField(max_length=16, default="proposed")
    drafter = models.CharField(max_length=32)
    invocation_ref = models.UUIDField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    used_message = models.ForeignKey(
        Message,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A message has at most one live suggestion; decisions name their human."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(state__in=SUGGESTION_STATE_VALUES),
                name="comms_suggestion_state",
            ),
            models.UniqueConstraint(
                fields=("in_reply_to",),
                condition=models.Q(state="proposed"),
                name="comms_suggestion_live_uniq",
            ),
        ]
