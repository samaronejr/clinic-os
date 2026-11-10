"""Additive net charge terms and many-to-many operational settlement lineage."""

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class PractitionerPayoutRule(TenantScopedModel):
    """Immutable per-service percentage (basis points) or fixed centavo policy."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    practitioner = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    service_type = models.ForeignKey(
        "scheduling.ServiceType", on_delete=models.PROTECT, null=True, blank=True
    )
    kind = models.CharField(max_length=8)
    value = models.PositiveBigIntegerField()
    command_id = models.UUIDField()
    policy_version = models.CharField(max_length=32, default="completion-net-v1")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Changing a rule creates a successor identity, not an in-place edit."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_rule_command_uniq"
            ),
        ]


class InvoiceTerms(TenantScopedModel):
    """Frozen gross, reductions, payer and practitioner policy before any allocation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    invoice = models.OneToOneField("billing.Invoice", on_delete=models.PROTECT)
    payer = models.CharField(max_length=8, default="patient")
    discount_minor = models.PositiveBigIntegerField(default=0)
    payer_adjustment_minor = models.PositiveBigIntegerField(default=0)
    net_minor = models.PositiveBigIntegerField()
    payout_rule = models.ForeignKey(
        PractitionerPayoutRule, on_delete=models.PROTECT, null=True, blank=True
    )
    completion_entry = models.OneToOneField(
        "billing.JournalEntry", on_delete=models.PROTECT, null=True, blank=True
    )
    completion_evidence = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)


class JournalSettlement(TenantScopedModel):
    """Observed money is not verified or posted money.

    Legacy Settlement and Receipt rows remain one-to-one and unchanged.
    A legacy link is an additive bridge, never a reparenting of those rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    legacy_settlement = models.OneToOneField(
        "billing.Settlement", on_delete=models.PROTECT, null=True, blank=True
    )
    amount_minor = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=16)
    account = models.CharField(max_length=16)
    evidence_reference = models.UUIDField()
    command_id = models.UUIDField()
    state = models.CharField(max_length=16, default="observed")
    verified_by_id = models.UUIDField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    entry = models.OneToOneField(
        "billing.JournalEntry", on_delete=models.PROTECT, null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Both retry identity and independently retained economic evidence converge."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_settlement_command_uniq"
            ),
            models.UniqueConstraint(
                fields=("book", "evidence_reference"),
                name="billing_settlement_evidence_uniq",
            ),
        ]


class SettlementAllocation(TenantScopedModel):
    """Immutable payment-to-invoice edge; neither side is one-to-one."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    settlement = models.ForeignKey(JournalSettlement, on_delete=models.PROTECT)
    terms = models.ForeignKey(InvoiceTerms, on_delete=models.PROTECT)
    amount_minor = models.PositiveBigIntegerField()
    repasse_minor = models.PositiveBigIntegerField(default=0)
    repasse_side = models.CharField(max_length=6, default="credit")
    command_id = models.UUIDField()
    group_id = models.UUIDField(null=True, blank=True)
    group_fingerprint = models.BinaryField(max_length=32, null=True, blank=True)
    entry = models.OneToOneField(
        "billing.JournalEntry", on_delete=models.PROTECT, null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """One allocation command identifies one exact immutable edge."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_allocation_command_uniq"
            ),
            models.UniqueConstraint(
                fields=("book", "group_id", "terms"),
                condition=models.Q(group_id__isnull=False),
                name="billing_allocation_group_terms_uniq",
            ),
        ]
