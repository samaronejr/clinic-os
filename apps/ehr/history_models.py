"""Append-only longitudinal entries and explicit, attributable assessment states."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class HistoryAssessment(TenantScopedModel):
    """One immutable category revision, including explicit absence declarations."""

    class Kind(models.TextChoices):
        """Independent clinical context categories."""

        PROBLEM = "problem", "Problemas"
        ALLERGY = "allergy", "Alergias"

    class State(models.TextChoices):
        """An empty table is never evidence of absence."""

        NOT_ASSESSED = "not_assessed", "Não avaliado"
        NONE_DOCUMENTED = "none_documented", "Nenhum registro documentado"
        DOCUMENTED = "documented", "Registros documentados"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind)
    state = models.CharField(max_length=24, choices=State)
    revision = models.PositiveIntegerField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    author_label = models.CharField(max_length=150)
    reason = EncryptedTextField(purpose="ehr.historyassessment.reason")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Serialize revisions per clinic, patient and category."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "patient", "kind", "revision"),
                name="ehr_history_revision_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name="ehr_history_revision_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["problem", "allergy"]),
                name="ehr_history_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=["not_assessed", "none_documented", "documented"]
                ),
                name="ehr_history_state_check",
            ),
        ]


class EntryVersion(TenantScopedModel):
    """Shared immutable clinical value; provenance belongs to its exact assessment."""

    class Status(models.TextChoices):
        """Resolution never means deletion or absence of prior documentation."""

        ACTIVE = "active", "Ativo"
        RESOLVED = "resolved", "Resolvido"
        ENTERED_IN_ERROR = "entered_in_error", "Registrado por engano"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry_id = models.UUIDField(default=uuid.uuid4, editable=False)
    assessment = models.OneToOneField(HistoryAssessment, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    description = EncryptedTextField(purpose="ehr.entryversion.description")
    status = models.CharField(max_length=24, choices=Status)

    class Meta:
        """Keep the logical identity separate from the immutable revision identity."""

        abstract = True
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("entry_id", "version"), name="%(class)s_entry_version_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="%(class)s_version_positive"
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "pg_catalog.octet_length(description) > 0",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="%(class)s_description_required",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["active", "resolved", "entered_in_error"]
                ),
                name="%(class)s_status_check",
            ),
        ]


class Problem(EntryVersion):
    """A physician-entered problem version, with no diagnostic inference."""


class Allergy(EntryVersion):
    """A physician-entered allergy version, with no interaction engine."""
