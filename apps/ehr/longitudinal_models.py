"""Enveloped longitudinal facts with retained origin and explicit confirmation."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.ehr.longitudinal_values import CODES, ORIGINS, ProvenanceTag
from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.models import TenantScopedModel


class PatientClinicalItem(TenantScopedModel):
    """Shared immutable patient scope; only reviewed state can advance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    performer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    origin = models.CharField(max_length=24, choices=ProvenanceTag)
    provenance = models.CharField(max_length=24, choices=ProvenanceTag)
    revision = models.PositiveIntegerField(default=1)
    effective_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Share fields without introducing another tenant table."""

        abstract = True


class MedicationStatement(PatientClinicalItem):
    """A reported medication is not a prescription or an automatic confirmation."""

    class Status(models.TextChoices):
        """Unknown is an explicit state, never an inferred absence."""

        ACTIVE = "active", _("Active")
        STOPPED = "stopped", _("Stopped")
        UNKNOWN = "unknown", _("Unknown")

    details = EncryptedJSONField(purpose="ehr.medicationstatement.details")
    status = models.CharField(max_length=16, choices=Status)
    reconciled_at = models.DateTimeField(null=True)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        """Close the stored status and provenance vocabularies."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(status__in=["active", "stopped", "unknown"]),
                name="ehr_medication_status",
            ),
            models.CheckConstraint(
                condition=models.Q(origin__in=ORIGINS)
                & models.Q(provenance__in=ProvenanceTag.values)
                & models.Q(revision__gte=1),
                name="ehr_medication_provenance",
            ),
        ]


class Observation(PatientClinicalItem):
    """Values, coded answers and reference ranges exist only inside an envelope."""

    code_set_version = models.PositiveSmallIntegerField(default=1)
    kind = models.CharField(
        max_length=24, choices=[(k, v.label) for k, v in CODES.items()]
    )
    unit = models.CharField(max_length=16)
    value = EncryptedJSONField(purpose="ehr.observation.value")
    confirmed_at = models.DateTimeField(null=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        """The migration additionally binds each kind to its exact unit."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(code_set_version=1, kind__in=list(CODES)),
                name="ehr_observation_codes_v1",
            ),
            models.CheckConstraint(
                condition=models.Q(origin__in=ORIGINS)
                & models.Q(provenance__in=ProvenanceTag.values)
                & models.Q(revision__gte=1),
                name="ehr_observation_provenance",
            ),
        ]
