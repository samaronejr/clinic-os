"""Public reference snapshots and private, retained physician favorites."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.prescription.class_values import PrescriptionClass
from apps.tenancy.models import TenantScopedModel


class PrescriptionClassPolicy(models.Model):
    """Versioned D-21 policy, seeded by migration, never caller approval."""

    category = models.CharField(
        max_length=40, primary_key=True, choices=PrescriptionClass
    )
    electronic_allowed_from = models.DateField(null=True)
    requires_sncr = models.BooleanField()
    signature_level = models.CharField(max_length=16)
    paper_only_until_gate = models.BooleanField()

    class Meta:
        """Reject unknown policy rows and signature levels independently."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(category__in=PrescriptionClass.values),
                name="prescription_policy_class",
            ),
            models.CheckConstraint(
                condition=models.Q(signature_level__in=["qualified", "synthetic"]),
                name="prescription_policy_signature",
            ),
        ]

    def __str__(self) -> str:
        """Expose the public class code, never clinical content."""
        return self.category


class MedicationCatalogImport(models.Model):
    """An immutable offline source receipt; no drug-class inference is stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schema_version = models.PositiveSmallIntegerField(default=1)
    source_url = models.URLField(max_length=1000)
    retrieved_on = models.DateField()
    checksum = models.CharField(max_length=64, unique=True)
    loaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Pin the normalized schema and checksum representation."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(schema_version=1, checksum__regex=r"^[0-9a-f]{64}$"),
                name="prescription_catalog_source_shape",
            )
        ]

    def __str__(self) -> str:
        """Identify a public source receipt by checksum."""
        return self.checksum


class MedicationCatalogEntry(models.Model):
    """Public medication terminology, without a regulatory classification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(MedicationCatalogImport, on_delete=models.PROTECT)
    code = models.CharField(max_length=80)
    description = models.CharField(max_length=240)
    strength_form = models.CharField(max_length=160)

    class Meta:
        """Keep source versions instead of updating referenced entries."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("source", "code"), name="prescription_catalog_source_code"
            )
        ]
        ordering: ClassVar = ["description", "code", "id"]

    def __str__(self) -> str:
        """Show its source's public terminology key."""
        return self.code


class MedicationFavorite(TenantScopedModel):
    """A physician's clinic-scoped bookmark, removed by deactivation only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    entry = models.ForeignKey(MedicationCatalogEntry, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Reactivation reuses the retained bookmark."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "issuer", "entry"),
                name="prescription_favorite_issuer_entry",
            )
        ]
