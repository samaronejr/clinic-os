"""Encrypted claim snapshots, immutable source facts and guarded state machines."""

from __future__ import annotations

import uuid
from typing import ClassVar, Self

from django.conf import settings
from django.db import models

from apps.tenancy.fields import (
    EncryptedBytesField,
    EncryptedJSONField,
    EncryptedTextField,
)
from apps.tenancy.models import TenantScopedModel


class InsuranceRow(TenantScopedModel):
    """Each command is bound to its exact clinic and canonical input digest."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    command_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    objects: ClassVar[models.Manager[Self]] = models.Manager()

    class Meta(TenantScopedModel.Meta):
        """No concrete shared table; each command identity is independently unique."""

        abstract = True
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "command_id"), name="%(app_label)s_%(class)s_cmd"
            )
        ]


class Payer(InsuranceRow):
    """Operator identity; an ANS number is not connectivity authorization."""

    ans_registry = models.CharField(max_length=6)
    name = models.CharField(max_length=160)


class PayerContract(InsuranceRow):
    """Immutable payer contract version and minimum billing identity."""

    payer = models.ForeignKey(Payer, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    starts_on = models.DateField()
    ends_on = models.DateField(null=True)
    billing_identity = EncryptedJSONField(purpose="insurance.contract", empty=dict)
    webservice = models.BooleanField(default=False)
    eligibility_supported = models.BooleanField(default=False)
    authorization_supported = models.BooleanField(default=False)


class TussTerm(InsuranceRow):
    """Imported terminology provenance is retained per versioned code."""

    component_version = models.CharField(max_length=16)
    source_date = models.DateField()
    source_sha256 = models.CharField(max_length=64)
    table_code = models.CharField(max_length=2)
    code = models.CharField(max_length=10)
    description = models.CharField(max_length=500)

    class Meta(InsuranceRow.Meta):
        """A terminology code never silently changes within an imported component."""

        abstract = False
        constraints: ClassVar = [
            *InsuranceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=("clinic", "component_version", "table_code", "code"),
                name="insurance_tuss_version_code",
            ),
        ]


class PriceTable(InsuranceRow):
    """Versioned exact-centavo terms reference imported TUSS identities."""

    contract = models.ForeignKey(PayerContract, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    prices = EncryptedJSONField(purpose="insurance.prices", empty=dict)


class InsuranceMembership(InsuranceRow):
    """No plaintext card identifier shadows the encrypted patient membership."""

    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="insurance_memberships",
    )
    payer = models.ForeignKey(
        Payer, on_delete=models.PROTECT, related_name="claim_memberships"
    )
    registration = models.ForeignKey(
        "intake.InsuranceMembership",
        on_delete=models.PROTECT,
        related_name="claim_memberships",
    )
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    details = EncryptedJSONField(purpose="insurance.membership", empty=dict)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True)


class Guide(InsuranceRow):
    """Immutable encounter/procedure facts; a correction is a successor guide."""

    contract = models.ForeignKey(PayerContract, on_delete=models.PROTECT)
    membership = models.ForeignKey(InsuranceMembership, on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.PROTECT)
    price_table = models.ForeignKey(PriceTable, on_delete=models.PROTECT)
    predecessor = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="successor"
    )
    kind = models.CharField(max_length=16)
    number = models.CharField(max_length=20)
    state = models.CharField(max_length=16, default="draft")
    facts = EncryptedJSONField(purpose="insurance.guide", empty=dict)
    total_minor = models.PositiveBigIntegerField()


class Batch(InsuranceRow):
    """The pinned XML bytes and ordered guide identities are immutable from creation."""

    contract = models.ForeignKey(PayerContract, on_delete=models.PROTECT)
    number = models.CharField(max_length=12)
    state = models.CharField(max_length=16, default="draft")
    guide_ids = models.JSONField()
    versions = models.JSONField()
    xml = EncryptedTextField(purpose="insurance.batch")
    content_sha256 = models.CharField(max_length=64)


class Submission(InsuranceRow):
    """One retained export/send attempt; receipt is not adjudication or payment."""

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT)
    channel = models.CharField(max_length=24)
    content_sha256 = models.CharField(max_length=64)


class BatchGuide(InsuranceRow):
    """One guide version belongs to exactly one batch; resubmit a successor instead."""

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT)
    guide = models.OneToOneField(Guide, on_delete=models.PROTECT)


class Return(InsuranceRow):
    """A source file is retained once per batch and exact-byte fingerprint."""

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT)
    source_sha256 = models.CharField(max_length=64)
    xml = EncryptedBytesField(purpose="insurance.return")

    class Meta(InsuranceRow.Meta):
        """Duplicate return files cannot create duplicate adjudication facts."""

        abstract = False
        constraints: ClassVar = [
            *InsuranceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=("batch", "source_sha256"), name="insurance_return_file"
            ),
        ]


class Glosa(InsuranceRow):
    """One immutable adjudicated guide line, including partial and full denial."""

    source = models.ForeignKey(Return, on_delete=models.PROTECT)
    guide = models.ForeignKey(Guide, on_delete=models.PROTECT)
    sequence = models.PositiveIntegerField()
    charged_minor = models.PositiveBigIntegerField()
    accepted_minor = models.PositiveBigIntegerField()
    denied_minor = models.PositiveBigIntegerField()
    code = models.CharField(max_length=4, blank=True)
    detail = EncryptedJSONField(purpose="insurance.adjudication", empty=dict)

    @property
    def decision(self) -> str:
        """Payment and acknowledgment are never inferred from adjudication."""
        if self.denied_minor == 0:
            return "accepted"
        return "denied" if self.accepted_minor == 0 else "partially_accepted"


class Appeal(InsuranceRow):
    """Finance drafts never approve new clinical assertions on a clinician's behalf."""

    glosa = models.ForeignKey(Glosa, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default="draft")
    body = EncryptedTextField(purpose="insurance.appeal")
    clinical_assertions = EncryptedTextField(
        purpose="insurance.appeal_assertions", null=True
    )
    content_sha256 = models.CharField(max_length=64)


class ClinicalApproval(InsuranceRow):
    """Dedicated physician approval is bound to the exact immutable appeal bytes."""

    appeal = models.OneToOneField(Appeal, on_delete=models.PROTECT)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    content_sha256 = models.CharField(max_length=64)


class PaymentMatch(InsuranceRow):
    """Only independently verified insurer money is allocated into the journal."""

    guide = models.ForeignKey(Guide, on_delete=models.PROTECT)
    settlement = models.ForeignKey(
        "billing.JournalSettlement", on_delete=models.PROTECT
    )
    allocation = models.OneToOneField(
        "billing.SettlementAllocation", on_delete=models.PROTECT
    )
    amount_minor = models.PositiveBigIntegerField()


class PayerRequest(InsuranceRow):
    """Synthetic eligibility/authorization workflow, not a clinical care decision."""

    membership = models.ForeignKey(InsuranceMembership, on_delete=models.PROTECT)
    appointment = models.ForeignKey(
        "scheduling.Appointment", on_delete=models.PROTECT, related_name="payer_checks"
    )
    contract = models.ForeignKey(PayerContract, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    outcome = models.CharField(max_length=16, default="unknown")
    authorization_reference = models.CharField(max_length=64, blank=True, default="")
    task = models.ForeignKey("workflows.Task", on_delete=models.PROTECT)
    evidence = EncryptedJSONField(purpose="insurance.request", empty=dict)


class CommandReceipt(InsuranceRow):
    """Every transition has an immutable idempotency and audit-bound identity."""

    subject_id = models.UUIDField()
    kind = models.CharField(max_length=32)
    next_state = models.CharField(max_length=16)
