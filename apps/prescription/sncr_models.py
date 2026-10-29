"""Immutable issuance receipts and protected SNCR documentation imports."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.prescription.class_values import PrescriptionClass
from apps.tenancy.fields import EncryptedJSONField
from apps.tenancy.models import TenantScopedModel


class SncrNumberAllocation(TenantScopedModel):
    """Owner-imported recording, never production numbering authority."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    category = models.CharField(max_length=40, choices=PrescriptionClass)
    request_digest = models.CharField(max_length=64)
    recording = EncryptedJSONField(
        purpose="prescription.sncrnumberallocation.recording"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Deduplicate exact recordings without indexing prescriber identifiers."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "issuer", "category", "request_digest"),
                name="prescription_sncr_recording_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(request_digest__regex=r"^[0-9a-f]{64}$"),
                name="prescription_sncr_recording_digest",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=PrescriptionClass.values)
                & ~models.Q(category__in=["simple", "synthetic_non_controlled"]),
                name="prescription_sncr_recording_class",
            ),
        ]


class PrescriptionIssuance(TenantScopedModel):
    """One command result per verified document; synthetic never means issued."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    document = models.OneToOneField(
        "prescription.PrescriptionDocument", on_delete=models.PROTECT
    )
    signature = models.OneToOneField(
        "prescription.SignatureOperation", on_delete=models.PROTECT
    )
    command_id = models.UUIDField()
    state = models.CharField(max_length=24, default="rehearsal_complete")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Enforce idempotency independently of application-level locking."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "command_id"), name="prescription_issuance_command"
            ),
            models.CheckConstraint(
                condition=models.Q(state="rehearsal_complete"),
                name="prescription_issuance_synthetic_only",
            ),
        ]
