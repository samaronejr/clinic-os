"""Immutable purpose-specific consent history; revocation is a separate event."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class ConsentText(TenantScopedModel):
    """A clinic-published exact text; every overlay creates another version."""

    class Purpose(models.TextChoices):
        """No blanket care-processing or messaging permission is represented."""

        TELECONSULTATION = "teleconsultation", "Teleconsulta"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    purpose = models.CharField(max_length=32, choices=Purpose)
    version = models.PositiveIntegerField()
    language = models.CharField(max_length=16, default="pt-BR")
    text = EncryptedTextField(purpose="consent.consenttext.text")
    digest = models.CharField(max_length=64)
    published_by = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Allocate one monotonically increasing series per clinic and purpose."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "purpose", "version"),
                name="consent_text_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    purpose="teleconsultation", language="pt-BR", version__gte=1
                ),
                name="consent_text_scope_check",
            ),
        ]


class ConsentAcceptance(TenantScopedModel):
    """One patient action bound to an enrollment and an immutable text version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    text = models.ForeignKey(ConsentText, on_delete=models.PROTECT)
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    authority = models.CharField(max_length=32, default="patient_explicit_action")
    accepted_at = models.DateTimeField()

    class Meta(TenantScopedModel.Meta):
        """Retries converge; authority cannot be claimed by staff."""

        abstract = False
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("enrollment", "text"), name="consent_acceptance_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(authority="patient_explicit_action"),
                name="consent_authority_check",
            ),
        ]


class ConsentRevocation(TenantScopedModel):
    """Retained patient revocation, never a destructive record operation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    acceptance = models.OneToOneField(
        ConsentAcceptance, on_delete=models.PROTECT, related_name="revocation"
    )
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT
    )
    revoked_at = models.DateTimeField()
