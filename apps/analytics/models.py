"""Immutable definitions, rebuilt projections and accountable saved reports."""

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class MetricDefinitionVersion(TenantScopedModel):
    """A clinic's immutable copy of a reviewed catalog definition."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    metric_id = models.CharField(max_length=32)
    version = models.PositiveIntegerField(default=1)
    specification = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        """Definitions cannot silently replace a version's meaning."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "metric_id", "version"),
                name="analytics_definition_version",
            ),
        ]


class ReadModelRefresh(TenantScopedModel):
    """Rebuildable aggregate snapshots contain no patient identifiers or text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    user = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    definition = models.ForeignKey(MetricDefinitionVersion, on_delete=models.PROTECT)
    authorization_version = models.CharField(max_length=64)
    descriptor = models.JSONField()
    command_id = models.UUIDField()
    rows = models.JSONField()
    quality = models.CharField(max_length=16)
    as_of = models.DateTimeField(default=timezone.now)

    class Meta:
        """The worker's command id is its replay identity."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "user", "command_id"),
                name="analytics_refresh_command",
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=("clinic", "user", "definition", "-as_of"),
                name="analytics_refresh_cache",
            ),
        ]


class SavedReport(TenantScopedModel):
    """Human-selected query settings, never arbitrary SQL or a clinical export."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    user = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    definition = models.ForeignKey(MetricDefinitionVersion, on_delete=models.PROTECT)
    descriptor = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)


class AnalystReview(TenantScopedModel):
    """A human decision binds immutable model output and permission version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    user = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    invocation = models.OneToOneField("ai.AIInvocation", on_delete=models.PROTECT)
    descriptor = models.JSONField()
    authorization_version = models.CharField(max_length=64)
    decision = models.CharField(max_length=8)
    created_at = models.DateTimeField(default=timezone.now)
