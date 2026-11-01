"""Tenant-scoped inference provenance and protected artifacts."""

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.ai.contracts import PURPOSES
from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class AIArtifact(TenantScopedModel):
    """Content exists only in a purpose-bound envelope, never in metering."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    patient_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", null=True, on_delete=models.PROTECT
    )
    purpose = models.CharField(max_length=24)
    method = models.CharField(max_length=12, default="complete")
    kind = models.CharField(max_length=8, default="input")
    content = EncryptedTextField(purpose="ai.artifact.content.v1", null=True)
    purged_at = models.DateTimeField(null=True)
    retention_class = models.CharField(max_length=24)
    expires_at = models.DateTimeField()
    invocation = models.OneToOneField(
        "ai.AIInvocation", null=True, on_delete=models.PROTECT, related_name="output"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Bind ciphertext provenance without constraining plaintext in SQL."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_artifact_scope_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(purpose__in=PURPOSES)
                & models.Q(method__in=("complete", "stream", "transcribe", "extract"))
                & (
                    models.Q(
                        kind="input",
                        invocation__isnull=True,
                        retention_class="transient_input",
                    )
                    | models.Q(
                        kind="output",
                        invocation__isnull=False,
                        retention_class__in=("review_draft", "transcript_partial"),
                    )
                ),
                name="ai_artifact_values",
            ),
        ]


class AIInvocation(TenantScopedModel):
    """A content-free metering ledger; one reservation per immutable input."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=24)
    configuration = models.ForeignKey("ai.ModelConfiguration", on_delete=models.PROTECT)
    config_version = models.PositiveIntegerField()
    prompt_sha256 = models.CharField(max_length=64)
    inputs_ref = models.OneToOneField(
        AIArtifact, on_delete=models.PROTECT, related_name="requested_invocation"
    )
    operation = models.OneToOneField(
        "comms.IntegrationOperation", null=True, on_delete=models.PROTECT
    )
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    tokens_cached = models.PositiveIntegerField(default=0)
    audio_seconds = models.PositiveIntegerField(default=0)
    limit_tokens_in = models.PositiveIntegerField()
    limit_tokens_out = models.PositiveIntegerField()
    limit_audio_seconds = models.PositiveIntegerField()
    reserved_micros = models.PositiveBigIntegerField()
    cost_micros = models.PositiveBigIntegerField(default=0)
    cost_minor = models.PositiveBigIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default="reserved")
    reason_code = models.CharField(max_length=32, blank=True)
    settled_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep monetary, usage, and terminal-state domains exact."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="ai_invocation_scope_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(purpose__in=PURPOSES)
                & models.Q(status__in=("reserved", "succeeded", "failed"))
                & models.Q(tokens_cached__lte=models.F("tokens_in"))
                & models.Q(tokens_in__lte=models.F("limit_tokens_in"))
                & models.Q(tokens_out__lte=models.F("limit_tokens_out"))
                & models.Q(audio_seconds__lte=models.F("limit_audio_seconds"))
                & models.Q(cost_micros__lte=models.F("reserved_micros"))
                & models.Q(limit_tokens_in__lte=1_000_000)
                & models.Q(limit_tokens_out__lte=1_000_000)
                & models.Q(limit_audio_seconds__lte=1_000_000)
                & models.Q(reserved_micros__lte=10**15),
                name="ai_invocation_values",
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=("organization", "created_at", "purpose"),
                name="ai_budget_lookup",
            )
        ]


class AICircuit(TenantScopedModel):
    """A configuration circuit belongs to an organization, not another tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.ForeignKey("ai.ModelConfiguration", on_delete=models.PROTECT)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)

    class Meta:
        """Retain closed history while allowing only one open circuit."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "configuration"),
                condition=models.Q(closed_at__isnull=True),
                name="ai_circuit_config_uniq",
            )
        ]
