"""Fiscal evidence is separate from payment receipts and operational postings."""

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.tenancy.fields import (
    EncryptedBytesField,
    EncryptedJSONField,
    EncryptedTextField,
)
from apps.tenancy.models import TenantScopedModel


class FiscalConfiguration(TenantScopedModel):
    """Owner-retained version of an external review, never a runtime approval."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    legal_entity_reference = models.UUIDField()
    legal_entity = EncryptedJSONField(purpose="billing.fiscal.legal_entity")
    provider = models.CharField(max_length=16)
    municipality_code = models.CharField(max_length=7)
    service_code = models.CharField(max_length=32)
    tax_regime = models.CharField(max_length=64)
    ibs_cbs_applicability = models.CharField(max_length=16)
    tax_fields = EncryptedJSONField(purpose="billing.fiscal.configuration_taxes")
    review_reference = models.UUIDField(null=True, blank=True)
    review_kind = models.CharField(max_length=24)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    cancellation_window_seconds = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)


class FiscalDocument(TenantScopedModel):
    """Exact reviewed terms and encrypted artifacts; a rehearsal is not NFS-e."""

    class Status(models.TextChoices):
        """Submission is not authorization, and rejection is not a receipt."""

        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        AUTHORIZED = "authorized", "Authorized"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    configuration = models.ForeignKey(FiscalConfiguration, on_delete=models.PROTECT)
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.PROTECT)
    legal_entity_reference = models.UUIDField()
    provider = models.CharField(max_length=16)
    municipality_code = models.CharField(max_length=7)
    service_code = models.CharField(max_length=32)
    tax_fields = EncryptedJSONField(purpose="billing.fiscal.configuration_taxes")
    amount_minor = models.PositiveBigIntegerField()
    rehearsal = models.BooleanField()
    status = models.CharField(max_length=16, choices=Status, default=Status.DRAFT)
    create_command_id = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32)
    last_command_id = models.UUIDField()
    dps_id = EncryptedTextField(purpose="billing.fiscal.dps_id")
    request_xml = EncryptedBytesField(purpose="billing.fiscal.request_xml")
    provider_reference = EncryptedTextField(
        purpose="billing.fiscal.provider_reference", null=True, blank=True
    )
    xml_payload = EncryptedBytesField(
        purpose="billing.fiscal.xml", null=True, blank=True
    )
    pdf_payload = EncryptedBytesField(
        purpose="billing.fiscal.pdf", null=True, blank=True
    )
    cancellation_payload = EncryptedBytesField(
        purpose="billing.fiscal.cancellation", null=True, blank=True
    )
    # References select this row's encrypted bytes, never a public URL or object key.
    xml_reference = models.UUIDField(null=True, blank=True)
    pdf_reference = models.UUIDField(null=True, blank=True)
    rejection_code = models.CharField(max_length=16, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    cancel_until = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Command identity and fiscal lifecycle are bounded at the database."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "create_command_id"),
                name="billing_fiscal_create_command_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "draft",
                        "submitted",
                        "authorized",
                        "rejected",
                        "cancelled",
                    ],
                    amount_minor__gt=0,
                ),
                name="billing_fiscal_document_terms",
            ),
        ]


class FiscalTransition(TenantScopedModel):
    """Database-written command receipts; no XML, recipient or tax text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(FiscalDocument, on_delete=models.PROTECT)
    command_id = models.UUIDField()
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    actor_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """A command cannot affect another document or name a second transition."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "command_id"),
                name="billing_fiscal_transition_command_uniq",
            ),
        ]
