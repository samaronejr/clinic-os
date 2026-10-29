"""Retained versioned bundles and enveloped specialty records."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from apps.ehr.models import SpecialtyTemplate
    from apps.intake.models import QuestionnaireTemplate


class SpecialtyPack(TenantScopedModel):
    """An installed bundle snapshot; retiring it never removes clinical history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    key = models.SlugField(max_length=32)
    version = models.PositiveIntegerField()
    position = models.PositiveIntegerField(default=1)
    template = models.OneToOneField("ehr.SpecialtyTemplate", on_delete=models.PROTECT)
    questionnaire = models.OneToOneField(
        "intake.QuestionnaireTemplate", on_delete=models.PROTECT
    )
    calculators = models.JSONField(default=list)
    license_ref = models.CharField(max_length=160)
    clinical_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    installed_at = models.DateTimeField(auto_now_add=True)
    uninstalled_at = models.DateTimeField(null=True)

    @property
    def templates(self) -> tuple[SpecialtyTemplate, ...]:
        """Expose the exact template bundle, never a newest-version lookup."""
        return (self.template,)

    @property
    def questionnaires(self) -> tuple[QuestionnaireTemplate, ...]:
        """Expose the retained questionnaire version for this bundle."""
        return (self.questionnaire,)

    class Meta:
        """Only one retained snapshot of each exact bundle can be installed."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "key", "version"), name="ehr_pack_version_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    key__in=["general", "pediatrics", "psychiatry", "procedures"],
                    version__gte=1,
                ),
                name="ehr_pack_catalog_check",
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=4),
                name="ehr_pack_position_check",
            ),
        ]


class SpecialtyEntry(TenantScopedModel):
    """Append-only reported immunizations or a procedure plan with source links."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    pack = models.ForeignKey(SpecialtyPack, on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    details = EncryptedJSONField(purpose="ehr.specialtyentry.details")
    consent_document = models.ForeignKey(
        "ehr.ClinicalAttachment", on_delete=models.PROTECT, null=True, related_name="+"
    )
    images = models.JSONField(default=list)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """A specialty entry is never an inferred consent or immunization."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(kind__in=["immunization", "procedure"]),
                name="ehr_specialty_entry_kind",
            )
        ]
