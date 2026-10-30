"""Retained clinic-scoped registrations, blinded ratings and model-change decisions."""

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.models import TenantScopedModel


class EvaluationProtocol(TenantScopedModel):
    """An immutable threshold/sample-size registration predating every scored run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    capability = models.CharField(max_length=5)
    version = models.PositiveIntegerField()
    corpus_version = models.CharField(max_length=32)
    corpus_sha256 = models.CharField(max_length=64)
    case_manifest = models.JSONField()
    thresholds_document = models.TextField()
    thresholds_sha256 = models.CharField(max_length=64)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    clinical_owner_ref = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Only a new version may change thresholds or corpus identity."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "capability", "version"),
                name="ai_eval_protocol_version",
            )
        ]


class EvaluationRun(TenantScopedModel):
    """Encrypted observations never enter the model registry or telemetry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    protocol = models.ForeignKey(EvaluationProtocol, on_delete=models.PROTECT)
    configuration = models.ForeignKey("ai.ModelConfiguration", on_delete=models.PROTECT)
    configuration_version = models.PositiveIntegerField()
    versions = models.JSONField()
    rater_one = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    rater_two = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    adjudicator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    sample_results = EncryptedJSONField(purpose="ai.evaluation.samples.v1", null=True)
    automatic_failures = models.PositiveSmallIntegerField(default=0)
    scored_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=8, default="pending")
    finished_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class EvaluationRating(TenantScopedModel):
    """One immutable structured review, with no rater-supplied clinical prose."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    run = models.ForeignKey(
        EvaluationRun, on_delete=models.PROTECT, related_name="ratings"
    )
    case_id = models.CharField(max_length=64)
    slot = models.CharField(max_length=16)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    labels = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A human cannot fill two roles or rewrite a first independent vote."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("run", "case_id", "slot"), name="ai_eval_rating_slot"
            ),
            models.UniqueConstraint(
                fields=("run", "case_id", "user"), name="ai_eval_rating_human"
            ),
        ]


class ModelChangeRequest(TenantScopedModel):
    """An owner decision binds the exact candidate, protocol and completed run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    configuration = models.ForeignKey("ai.ModelConfiguration", on_delete=models.PROTECT)
    run = models.ForeignKey(EvaluationRun, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    status = models.CharField(max_length=9, default="requested")
    approval_ref = models.CharField(max_length=128, blank=True)
    decided_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
