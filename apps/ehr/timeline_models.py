"""Metadata-only projection and durable patient-scoped refresh acknowledgements."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models

from apps.tenancy.models import TenantScopedModel


class TimelineRefresh(TenantScopedModel):
    """One durable revision fence per patient and exact clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField(default=1)
    refreshed_revision = models.PositiveBigIntegerField(default=0)
    refreshed_at = models.DateTimeField(null=True)

    class Meta:
        """A source edit cannot be acknowledged by an older refresh."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "patient"), name="ehr_timeline_refresh_patient"
            ),
            models.CheckConstraint(
                condition=models.Q(refreshed_revision__lte=models.F("revision")),
                name="ehr_timeline_refresh_fence",
            ),
        ]


class TimelineProjection(TenantScopedModel):
    """No narrative, names, values, hidden counts or cached authorization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    source_id = models.UUIDField()
    kind = models.CharField(max_length=24)
    effective_at = models.DateTimeField()
    provenance = models.CharField(max_length=24)
    source_revision = models.PositiveIntegerField(default=1)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT, null=True)
    episode = models.ForeignKey("ehr.Episode", on_delete=models.PROTECT, null=True)

    class Meta:
        """Refreshing a source updates one retained projection identity."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("kind", "source_id"), name="ehr_timeline_source_unique"
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=("clinic", "patient", "-effective_at", "id"),
                name="ehr_timeline_patient_time",
            )
        ]
