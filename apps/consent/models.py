"""Immutable purpose-specific consent history; revocation is a separate event."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class ConsentPurpose(models.TextChoices):
    """Every purpose a patient may accept or refuse; none is a blanket grant."""

    TELECONSULTATION = "teleconsultation", _("Teleconsultation")
    CONSULTATION_RECORDING = "consultation_recording", _("Consultation recording")
    AI_ASSISTANCE = "ai_assistance", _("AI assistance")
    TRANSACTIONAL_MESSAGING = "transactional_messaging", _("Transactional messaging")
    MARKETING = "marketing", _("Marketing")
    RESEARCH_MODEL_IMPROVEMENT = (
        "research_model_improvement",
        _("Research and model improvement"),
    )


class NoticeTopic(models.TextChoices):
    """Informational-notice topics; a notice informs, it never authorizes."""

    CARE_PROCESSING = "care_processing", _("Care data processing")
    CONSULTATION_RECORDING = "consultation_recording", _("Consultation recording")
    AI_USE = "ai_use", _("Use of AI in care")
    TELECONSULTATION = "teleconsultation", _("Teleconsultation")


class ConsentText(TenantScopedModel):
    """A clinic-published exact text; every overlay creates another version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    purpose = models.CharField(max_length=32, choices=ConsentPurpose)
    version = models.PositiveIntegerField()
    language = models.CharField(max_length=16, default="pt-BR")
    text = EncryptedTextField(purpose="consent.consenttext.text")
    digest = models.CharField(max_length=64)
    published_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Allocate one monotonically increasing series per clinic and purpose."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "purpose", "version"),
                name="consent_text_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    purpose__in=ConsentPurpose.values,
                    language="pt-BR",
                    version__gte=1,
                ),
                name="consent_text_scope_check",
            ),
        ]


class ConsentAcceptance(TenantScopedModel):
    """One patient action bound to an enrollment and an immutable text version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    text = models.ForeignKey(ConsentText, on_delete=models.PROTECT)
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    authority = models.CharField(max_length=32, default="patient_explicit_action")
    accepted_at = models.DateTimeField()

    class Meta(TenantScopedModel.Meta):
        """Retries converge; authority cannot be claimed by staff."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("enrollment", "text"), name="consent_acceptance_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(authority="patient_explicit_action"),
                name="consent_authority_check",
            ),
        ]


class ConsentRevocation(TenantScopedModel):
    """Retained patient revocation, never a destructive record operation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    acceptance = models.OneToOneField(
        ConsentAcceptance, on_delete=models.PROTECT, related_name="revocation"
    )
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    revoked_at = models.DateTimeField()


class RefusalRecord(TenantScopedModel):
    """One deliberate patient refusal of an offered text; never silent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    text = models.ForeignKey(ConsentText, on_delete=models.PROTECT)
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    authority = models.CharField(max_length=32, default="patient_explicit_action")
    refused_at = models.DateTimeField()

    class Meta(TenantScopedModel.Meta):
        """A refusal binds the same displayed version as an acceptance."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("enrollment", "text"), name="consent_refusal_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(authority="patient_explicit_action"),
                name="consent_refusal_authority_check",
            ),
        ]


class NoticeVersion(TenantScopedModel):
    """A clinic-published information-only notice; it authorizes nothing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    topic = models.CharField(max_length=32, choices=NoticeTopic)
    version = models.PositiveIntegerField()
    language = models.CharField(max_length=16, default="pt-BR")
    text = EncryptedTextField(purpose="consent.noticeversion.text")
    digest = models.CharField(max_length=64)
    published_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """One monotonically increasing series per clinic and notice topic."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "topic", "version"),
                name="consent_notice_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    topic__in=NoticeTopic.values,
                    language="pt-BR",
                    version__gte=1,
                ),
                name="consent_notice_scope_check",
            ),
        ]


class ParticipantKind(models.TextChoices):
    """Every voice class that may be present without being the patient."""

    CAREGIVER = "caregiver", _("Caregiver")
    COMPANION = "companion", _("Companion")
    INTERPRETER = "interpreter", _("Interpreter")


class ParticipantAcknowledgment(TenantScopedModel):
    """Staff-recorded recording notice for a non-patient voice in a session.

    Caregivers, companions and interpreters are never turned into patient
    records; only their participant kind and the clinician who delivered
    the notice are stored.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    session = models.ForeignKey(
        "ehr.Encounter",
        on_delete=models.PROTECT,
        help_text="The clinical session (encounter) the voice participates in.",
    )
    participant_kind = models.CharField(max_length=16, choices=ParticipantKind)
    acknowledged_by_clinician = models.ForeignKey(
        "identity.User", on_delete=models.PROTECT
    )
    acknowledged_at = models.DateTimeField()

    class Meta(TenantScopedModel.Meta):
        """One acknowledgment per participant kind per session."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("session", "participant_kind"),
                name="consent_participant_ack_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(participant_kind__in=ParticipantKind.values),
                name="consent_participant_kind_check",
            ),
        ]


class AIUseDisclosure(TenantScopedModel):
    """One clinician attestation per encounter: informed state and refusal.

    ``informed`` records that the clinician told the patient AI will assist;
    ``refused`` records the patient's refusal. ``refused`` can only be true
    when ``informed`` is also true. Care never depends on either value.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    encounter = models.OneToOneField("ehr.Encounter", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    informed = models.BooleanField()
    refused = models.BooleanField()
    recorded_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    recorded_at = models.DateTimeField()

    class Meta(TenantScopedModel.Meta):
        """One immutable disclosure per encounter; refusal implies informed."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(refused=False) | models.Q(informed=True),
                name="consent_ai_disclosure_coherent_check",
            ),
        ]
