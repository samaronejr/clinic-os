"""Owner-written platform model configurations and the global kill switch."""

import uuid
from typing import ClassVar

from django.db import models

from apps.ai.contracts import PURPOSES


class ModelConfiguration(models.Model):
    """Immutable inference terms once approved; runtime has SELECT only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField(max_length=24, choices=[(p, p) for p in PURPOSES])
    capability_key = models.CharField(max_length=64)
    provider = models.CharField(max_length=32)
    model_id = models.CharField(max_length=128)
    region = models.CharField(max_length=64)
    jurisdiction_class = models.CharField(max_length=8)
    endpoint = models.URLField(max_length=255)
    retention_terms = models.TextField()
    training_terms = models.TextField()
    input_micros = models.PositiveIntegerField(default=0)
    output_micros = models.PositiveIntegerField(default=0)
    cached_micros = models.PositiveIntegerField(default=0)
    audio_micros = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField()
    approval_ref = models.CharField(max_length=128, blank=True)
    route = models.CharField(
        max_length=8, choices=(("primary", "Primary"), ("fallback", "Fallback"))
    )
    state = models.CharField(
        max_length=8,
        choices=[(s, s) for s in ("draft", "approved", "active", "retired")],
        default="draft",
    )
    timeout_seconds = models.PositiveSmallIntegerField(default=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Pin version identity and one active primary per purpose."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("purpose", "version"), name="ai_configuration_version_uniq"
            ),
            models.UniqueConstraint(
                fields=("purpose",),
                condition=models.Q(state="active", route="primary"),
                name="ai_configuration_primary_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(purpose__in=PURPOSES)
                & models.Q(state__in=("draft", "approved", "active", "retired"))
                & models.Q(route__in=("primary", "fallback"))
                & models.Q(capability_key__in=("asr", "llm_inference"))
                & models.Q(provider__in=("synthetic", "recorded_fixture"))
                & models.Q(version__gte=1)
                & models.Q(timeout_seconds__gte=1, timeout_seconds__lte=60)
                & models.Q(input_micros__lte=1_000_000)
                & models.Q(output_micros__lte=1_000_000)
                & models.Q(cached_micros__lte=1_000_000)
                & models.Q(audio_micros__lte=1_000_000),
                name="ai_configuration_values",
            ),
        ]

    def __str__(self) -> str:
        """Return only the technical configuration reference."""
        return str(self.pk)


class GatewaySwitch(models.Model):
    """An owner-controlled global stop, defaulting to disabled."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    enabled = models.BooleanField(default=False)
    approved_regions = models.JSONField(default=list)

    class Meta:
        """Only the singleton control row is meaningful."""

        constraints: ClassVar = [
            models.CheckConstraint(condition=models.Q(id=1), name="ai_switch_singleton")
        ]

    def __str__(self) -> str:
        """Identify the global control without serializing its fields."""
        return "AI gateway global control"
