"""Clinic-only operational books and sealed double-entry facts."""

import uuid
from typing import ClassVar, Final

from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel

# This closed chart has no Clinic Ops subscription or platform revenue account.
CHART: Final = (
    "bank",
    "petty_cash",
    "psp_clearing",
    "payout_in_transit",
    "patient_ar",
    "insurer_ar",
    "patient_advances",
    "refunds_payable",
    "unidentified_receipts",
    "insurer_overpayments",
    "practitioner_payables",
    "taxes_payable",
    "service_revenue",
    "contra_revenue",
    "discounts",
    "payer_adjustments",
    "psp_fees",
    "practitioner_compensation",
    "bad_debt",
    "suspense",
    "opening_balances",
)
POLICY_VERSION: Final = "completion-net-v1"


class JournalBook(TenantScopedModel):
    """One BRL book for a clinic's legal entity, never a SaaS book."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.OneToOneField("identity.Clinic", on_delete=models.PROTECT)
    policy_version = models.CharField(max_length=32, default=POLICY_VERSION)
    currency = models.CharField(max_length=3, default="BRL")
    created_at = models.DateTimeField(default=timezone.now, editable=False)


class Account(TenantScopedModel):
    """One database-seeded account from the closed minimum chart."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey(JournalBook, on_delete=models.PROTECT)
    code = models.CharField(max_length=32)

    class Meta:
        """Account identities never change or duplicate within a book."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "code"), name="billing_account_book_code_uniq"
            ),
        ]


class JournalEntry(TenantScopedModel):
    """A single economic fact, proposed then sealed; reversal links retain history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey(JournalBook, on_delete=models.PROTECT)
    source_kind = models.CharField(max_length=32)
    source_id = models.UUIDField()
    content_hash = models.BinaryField(max_length=32)
    state = models.CharField(max_length=16, default="proposed")
    reversal_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Delivery ids are not posting ids; economic fact identity is unique."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "source_kind", "source_id"),
                name="billing_entry_fact_uniq",
            ),
        ]


class JournalLine(TenantScopedModel):
    """Positive integer centavos plus side, with optional accountable dimensions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    side = models.CharField(max_length=6)
    amount_minor = models.PositiveBigIntegerField()
    practitioner_id = models.UUIDField(null=True, blank=True)
    owner_id = models.UUIDField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
