"""Immutable model generations, separated proposals and clinician provenance."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class AIDraft(TenantScopedModel):
    """One admitted generation retains its exact note, sources and model version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    version = models.ForeignKey("ehr.ClinicalDocumentVersion", on_delete=models.PROTECT)
    transcript = models.ForeignKey("scribe.TranscriptVersion", on_delete=models.PROTECT)
    consent = models.ForeignKey("consent.ConsentAcceptance", on_delete=models.PROTECT)
    invocation = models.OneToOneField("ai.AIInvocation", on_delete=models.PROTECT)
    requested_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    request_key = models.UUIDField()
    generation = models.PositiveIntegerField()
    base_revision = models.PositiveIntegerField()
    base_epochs = models.JSONField()
    base_content = EncryptedJSONField(purpose="scribe.draft.base")
    sections = models.JSONField()
    source_refs = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Serialize generations and converge matching admission replays."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("version", "generation"), name="scribe_draft_generation"
            ),
            models.UniqueConstraint(
                fields=("version", "request_key"), name="scribe_draft_request_key"
            ),
        ]


class DraftSectionSuggestion(TenantScopedModel):
    """Suggestions never share the authored note's storage or writer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey("ehr.ClinicalDocumentVersion", on_delete=models.PROTECT)
    draft = models.ForeignKey(AIDraft, on_delete=models.PROTECT)
    section = models.CharField(max_length=16)
    text = EncryptedTextField(purpose="scribe.draft.suggestion")
    citations = models.JSONField()
    assertion_class = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """A generation produces at most one candidate for each note section."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("draft", "section"), name="scribe_draft_section"
            ),
        ]


class DraftActionProposal(TenantScopedModel):
    """Source-linked A0 order/task proposals cannot enter the note apply path."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey("ehr.ClinicalDocumentVersion", on_delete=models.PROTECT)
    draft = models.ForeignKey(AIDraft, on_delete=models.PROTECT)
    ordinal = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=8)
    text = EncryptedTextField(purpose="scribe.draft.proposal")
    citations = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Retain distinct source proposals without creating orders or tasks."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("draft", "ordinal"), name="scribe_draft_proposal"
            ),
        ]


class ReviewDecision(TenantScopedModel):
    """An appended encounter AI-use record; acceptance never hides AI origin."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey("ehr.ClinicalDocumentVersion", on_delete=models.PROTECT)
    suggestion = models.OneToOneField(DraftSectionSuggestion, on_delete=models.PROTECT)
    reviewed_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    command_id = models.UUIDField()
    request_digest = models.CharField(max_length=64)
    action = models.CharField(max_length=8)
    section = models.CharField(max_length=16)
    expected_revision = models.PositiveIntegerField()
    result_revision = models.PositiveIntegerField()
    result_sha256 = models.CharField(max_length=64)
    # Only rejection stores NULL; explicit deletion applies an encrypted "".
    applied_text = EncryptedJSONField(purpose="scribe.review.applied", null=True)
    capability_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """One immutable clinician decision and one exact command receipt."""

        abstract = False
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("version", "command_id"), name="scribe_review_command"
            ),
        ]
