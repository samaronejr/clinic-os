"""Reserved outgoing money and retained cash-count history."""

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.tenancy.models import TenantScopedModel


class Refund(TenantScopedModel):
    """Reserve an allocation before approval or dispatch; unknown retains the claim."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    allocation = models.ForeignKey(
        "billing.SettlementAllocation", on_delete=models.PROTECT, null=True, blank=True
    )
    settlement = models.ForeignKey(
        "billing.JournalSettlement", on_delete=models.PROTECT
    )
    amount_minor = models.PositiveBigIntegerField()
    command_id = models.UUIDField()
    kind = models.CharField(max_length=16, default="refund")
    state = models.CharField(max_length=16, default="requested")
    requested_by_id = models.UUIDField()
    approved_by_id = models.UUIDField(null=True, blank=True)
    approval_session = models.ForeignKey(
        "identity.SessionRecord", on_delete=models.PROTECT, null=True, blank=True
    )
    evidence_reference = models.UUIDField(null=True, blank=True)
    approval_entry = models.OneToOneField(
        "billing.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    repasse_entry = models.OneToOneField(
        "billing.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    repasse_reversed_minor = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """A request retry never reserves a second amount."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_refund_command_uniq"
            ),
        ]


class Payout(TenantScopedModel):
    """Reserve earned payables and retain ambiguous transfers in transit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    practitioner = models.ForeignKey("identity.User", on_delete=models.PROTECT)
    amount_minor = models.PositiveBigIntegerField()
    command_id = models.UUIDField()
    state = models.CharField(max_length=16, default="requested")
    requested_by_id = models.UUIDField()
    approved_by_id = models.UUIDField(null=True, blank=True)
    approval_session = models.ForeignKey(
        "identity.SessionRecord", on_delete=models.PROTECT, null=True, blank=True
    )
    evidence_reference = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """A payout command identifies one reserved amount and beneficiary."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_payout_command_uniq"
            ),
        ]


class CashSession(TenantScopedModel):
    """One counted cash drawer; close and reopen never erase previous counts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    command_id = models.UUIDField()
    last_command_id = models.UUIDField()
    state = models.CharField(max_length=8, default="open")
    counted_minor = models.PositiveBigIntegerField()
    expected_minor = models.BigIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1)
    reason = models.CharField(max_length=16, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """A book has at most one open drawer."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book",),
                condition=models.Q(state="open"),
                name="billing_cash_one_open",
            ),
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_cash_command_uniq"
            ),
        ]


class CashSessionEvent(TenantScopedModel):
    """Database-derived immutable count/reopen receipt with exact actor and session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    session = models.ForeignKey(CashSession, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    command_id = models.UUIDField()
    state = models.CharField(max_length=16)
    counted_minor = models.PositiveBigIntegerField()
    expected_minor = models.BigIntegerField()
    reason = models.CharField(max_length=16, default="")
    actor_id = models.UUIDField()
    verification_session_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Every drawer revision has one retained receipt."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("session", "revision"), name="billing_cash_event_uniq"
            ),
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_cash_event_command_uniq"
            ),
        ]
