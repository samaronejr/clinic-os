"""Stable encounter identities and explicitly versioned clinical drafts."""

from __future__ import annotations

import uuid
from hashlib import sha256
from typing import ClassVar, Final

import rfc8785
from django.conf import settings
from django.db import models

from apps.ehr.history_models import Allergy, HistoryAssessment, Problem
from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel

__all__ = ("Allergy", "HistoryAssessment", "Problem")

SOAP_FIELD_NAMES: Final = ("subjective", "objective", "assessment", "plan")


class SpecialtyTemplate(TenantScopedModel):
    """Immutable SOAP prompts; publishing creates a new version, never a replacement."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    key = models.SlugField(max_length=64)
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=160)
    prompts = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Name each exact clinic template version."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "key", "version"), name="ehr_template_version_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="ehr_template_version_positive"
            ),
        ]


class Encounter(TenantScopedModel):
    """One retained clinical identity per appointment, independent of cancellation."""

    class State(models.TextChoices):
        """Closed encounters cannot be reopened."""

        OPEN = "open", "Aberto"
        CLOSED = "closed", "Encerrado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    appointment = models.OneToOneField(
        "scheduling.Appointment", on_delete=models.PROTECT
    )
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    physician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, choices=State, default=State.OPEN)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Keep closure and timestamps coherent."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(state="open", closed_at__isnull=True)
                | models.Q(state="closed", closed_at__isnull=False),
                name="ehr_encounter_state_check",
            ),
        ]


class ClinicalDocument(TenantScopedModel):
    """Stable SOAP document identity; its content lives only in version rows."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, default="soap")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Start/resume converges on the encounter's SOAP document."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("encounter", "kind"), name="ehr_document_kind_uniq"
            ),
        ]


class ClinicalDocumentVersion(TenantScopedModel):
    """One authored version with atomic optimistic draft revisions."""

    class State(models.TextChoices):
        """Lifecycle vocabulary reserved for the record contract."""

        DRAFT = "draft", "Rascunho"
        FINALIZED = "finalized", "Finalizado"
        SUPERSEDED = "superseded", "Substituído"
        DISCARDED = "discarded", "Descartado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(ClinicalDocument, on_delete=models.PROTECT)
    template = models.ForeignKey(SpecialtyTemplate, on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    amendment_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="amendments",
    )
    amendment_reason = EncryptedTextField(
        purpose="ehr.clinicaldocumentversion.amendment_reason", null=True
    )
    version = models.PositiveIntegerField(default=1)
    revision = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=16, choices=State, default=State.DRAFT)
    # The SOAP body persists only as one tenant envelope; content_sha256 is
    # the plaintext digest the binding trigger compares across transitions.
    content = EncryptedJSONField(
        purpose="ehr.clinicaldocumentversion.content", null=True, empty={}
    )
    content_sha256 = models.CharField(max_length=64, blank=True, default="")
    content_digest = models.CharField(max_length=64, blank=True, default="")
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Database uniqueness protects both draft and current-version identities."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("document", "version"), name="ehr_document_version_uniq"
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(state="draft"),
                name="ehr_document_one_draft",
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(state="finalized"),
                name="ehr_document_one_current",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=["draft", "finalized", "superseded", "discarded"]
                ),
                name="ehr_version_state_check",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1, revision__gte=1),
                name="ehr_version_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(content_digest="")
                | models.Q(content_digest__regex=r"^[0-9a-f]{64}$"),
                name="ehr_version_digest_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=["finalized", "superseded"],
                        finalized_at__isnull=False,
                    )
                    & ~models.Q(content_digest="")
                )
                | (
                    models.Q(
                        state__in=["draft", "discarded"],
                        finalized_at__isnull=True,
                    )
                    & models.Q(content_digest="")
                ),
                name="ehr_version_finalized_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(amendment_of__isnull=True)
                    & models.Q(amendment_reason__isnull=True)
                )
                | (
                    models.Q(amendment_of__isnull=False)
                    & models.Q(amendment_reason__isnull=False)
                    & models.Q(version__gte=2)
                ),
                name="ehr_version_amendment_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(content__isnull=True, content_sha256="")
                    | (
                        models.Q(content__isnull=False)
                        & models.Q(content_sha256__regex=r"^[0-9a-f]{64}$")
                    )
                ),
                name="ehr_version_content_check",
            ),
        ]

    @property
    def soap(self) -> dict[str, str]:
        """Decrypt the SOAP body; discarded versions expose empty content."""
        stored = self.content
        if not isinstance(stored, dict):
            return {}
        return {key: str(stored.get(key, "")) for key in SOAP_FIELD_NAMES}

    @property
    def subjective(self) -> str:
        """Decrypt the subjective section for authorized readers."""
        return self.soap["subjective"]

    @property
    def objective(self) -> str:
        """Decrypt the objective section for authorized readers."""
        return self.soap["objective"]

    @property
    def assessment(self) -> str:
        """Decrypt the assessment section for authorized readers."""
        return self.soap["assessment"]

    @property
    def plan(self) -> str:
        """Decrypt the plan section for authorized readers."""
        return self.soap["plan"]

    def set_soap(self, content: dict[str, str]) -> None:
        """Store the SOAP body as one envelope plus its plaintext digest."""
        self.content = {key: content[key] for key in SOAP_FIELD_NAMES}
        self.content_sha256 = sha256(rfc8785.dumps(self.content)).hexdigest()


class ClinicalAttachment(TenantScopedModel):
    """One uploaded file kept private until validation marks it available."""

    class State(models.TextChoices):
        """Quarantined bytes are never served; rejection is terminal."""

        QUARANTINED = "quarantined", "Em verificação"
        AVAILABLE = "available", "Disponível"
        REJECTED = "rejected", "Rejeitado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    storage_key = models.CharField(max_length=64, unique=True)
    file_name = models.CharField(max_length=120)
    declared_type = models.CharField(max_length=64)
    detected_type = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State, default=State.QUARANTINED)
    scan_attempts = models.PositiveIntegerField(default=0)
    scan_reason = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Keep type, size and lifecycle vocabulary exact in the database."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(state__in=["quarantined", "available", "rejected"]),
                name="ehr_attachment_state_check",
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gte=1, size_bytes__lte=10485760),
                name="ehr_attachment_size_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    declared_type__in=[
                        "application/pdf",
                        "image/jpeg",
                        "image/png",
                    ]
                )
                & models.Q(
                    detected_type__in=[
                        "application/pdf",
                        "image/jpeg",
                        "image/png",
                    ]
                ),
                name="ehr_attachment_type_check",
            ),
            models.CheckConstraint(
                condition=models.Q(sha256__regex=r"^[0-9a-f]{64}$")
                & models.Q(storage_key__regex=r"^[0-9a-f]{64}$"),
                name="ehr_attachment_digest_check",
            ),
            models.CheckConstraint(
                condition=models.Q(scanned_at__isnull=True, state="quarantined")
                | models.Q(
                    scanned_at__isnull=False,
                    state__in=["available", "rejected"],
                ),
                name="ehr_attachment_scanned_check",
            ),
        ]


class EncounterIntakeReference(TenantScopedModel):
    """Reference the immutable submitted receipt, never the reopenable response row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    submission = models.ForeignKey(
        "intake.QuestionnaireEvent", on_delete=models.PROTECT
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Retain each exact submission only once."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("encounter", "submission"), name="ehr_intake_reference_uniq"
            ),
        ]
