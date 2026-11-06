"""Separate leads, immutable outreach receipts and optional patient feedback."""

import uuid
from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class ReferralSource(TenantScopedModel):
    """An administrative source, never a clinical audience dimension."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    code = models.CharField(max_length=40)
    name = EncryptedTextField(purpose="crm.referralsource.name")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One local nonclinical source code."""

        constraints: ClassVar = [
            models.UniqueConstraint(fields=("clinic", "code"), name="crm_source_code"),
        ]


class Lead(TenantScopedModel):
    """A prospect is not a patient and has its own bounded retention deadline."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    name = EncryptedTextField(purpose="crm.lead.name", null=True)
    email = EncryptedTextField(purpose="crm.lead.email", null=True)
    source = models.ForeignKey(ReferralSource, null=True, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", null=True, on_delete=models.PROTECT
    )
    state = models.CharField(max_length=16, default="active")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Redaction retains only the minimal nonidentifying conversion receipt."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=(
                    models.Q(state="active", enrollment__isnull=True)
                    | models.Q(state="converted", enrollment__isnull=False)
                    | models.Q(
                        state="redacted",
                        enrollment__isnull=True,
                        name__isnull=True,
                        email__isnull=True,
                    )
                ),
                name="crm_lead_state",
            ),
        ]


class MarketingPreference(TenantScopedModel):
    """Append-only channel choice made by the patient, never staff attestation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    channel = models.CharField(max_length=16)
    opted_in = models.BooleanField()
    contact_version = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Each opt-in binds a verified destination version; missing means no."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("enrollment", "channel", "revision"),
                name="crm_marketing_revision",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=("email", "sms", "whatsapp")),
                name="crm_marketing_channel",
            ),
        ]


class Campaign(TenantScopedModel):
    """Human-approved neutral content with no clinical targeting dimensions."""

    class State(models.TextChoices):
        """Batch lifecycle; delivery remains the comms operation's own receipt."""

        DRAFT = "draft", _("Draft")
        APPROVED = "approved", _("Approved")
        SENDING = "sending", _("Sending")
        SENT = "sent", _("Sent")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    title = EncryptedTextField(purpose="crm.campaign.title")
    template = models.ForeignKey("comms.TemplateVersion", on_delete=models.PROTECT)
    state = models.CharField(max_length=16, choices=State, default=State.DRAFT)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    excluded_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class CampaignSend(TenantScopedModel):
    """Immutable link to the comms marketing message; no content in this ledger."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    message = models.OneToOneField("comms.Message", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A repeated batch cannot queue the same recipient twice."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("campaign", "enrollment"), name="crm_send_recipient"
            ),
        ]


class SatisfactionSurvey(TenantScopedModel):
    """Optional post-visit score; no clinical or free-text answers are collected."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    appointment = models.OneToOneField(
        "scheduling.Appointment", on_delete=models.PROTECT
    )
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    score = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One explicit score from zero to ten, with no inferred sentiment."""

        constraints: ClassVar = [
            models.CheckConstraint(condition=models.Q(score__lte=10), name="crm_score"),
        ]


class PublicBookingProfile(TenantScopedModel):
    """Published real clinic copy, resolved from a code sent only in a POST body."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.OneToOneField("identity.Clinic", on_delete=models.PROTECT)
    code = models.CharField(max_length=40, unique=True)
    description = models.TextField(max_length=400)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
