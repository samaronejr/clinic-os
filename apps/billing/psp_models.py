"""Append-only sandbox policy, immutable requests, references and PSP evidence."""

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class PspPolicy(TenantScopedModel):
    """A successor policy changes future charges, never already frozen requests."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    max_installments = models.PositiveSmallIntegerField(default=1)
    command_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Preserve every explicitly authorized clinic policy revision."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "command_id"), name="billing_psp_policy_command_uniq"
            ),
        ]


class PspIntent(TenantScopedModel):
    """One economic target has one frozen provider idempotency key forever."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    book = models.ForeignKey("billing.JournalBook", on_delete=models.PROTECT)
    policy = models.ForeignKey(PspPolicy, on_delete=models.PROTECT)
    capability_version = models.ForeignKey(
        "providers.CapabilityVersion", on_delete=models.PROTECT
    )
    requested_by_id = models.UUIDField()
    dispatch_session = models.ForeignKey(
        "identity.SessionRecord", on_delete=models.PROTECT, null=True, blank=True
    )
    provider = models.CharField(max_length=64)
    kind = models.CharField(max_length=8)
    target_id = models.UUIDField()
    method = models.CharField(max_length=4)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="BRL")
    installments = models.PositiveSmallIntegerField(default=1)
    original_reference = models.CharField(max_length=128, default="", blank=True)
    payee_reference = models.CharField(max_length=32, default="", blank=True)
    original = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """A timeout can never mint a replacement charge, refund or payout."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("book", "kind", "target_id"), name="billing_psp_target_uniq"
            ),
        ]


class PspBinding(TenantScopedModel):
    """Bind the provider's reference exactly once after creation or status recovery."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intent = models.OneToOneField(PspIntent, on_delete=models.PROTECT)
    provider = models.CharField(max_length=64)
    provider_reference = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Neither another clinic nor another request may reuse a provider reference."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("provider", "provider_reference"),
                name="billing_psp_reference_uniq",
            ),
        ]


class PspEvidence(TenantScopedModel):
    """Raw bodies never persist; immutable facts and encrypted instructions do."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intent = models.ForeignKey(PspIntent, on_delete=models.PROTECT)
    provider = models.CharField(max_length=64)
    event_id = models.CharField(max_length=128)
    source = models.CharField(max_length=16)
    status = models.CharField(max_length=16)
    decision = models.CharField(max_length=16)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    fee_minor = models.PositiveBigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)
    payment_code = EncryptedTextField(
        purpose="billing.psp.payment_code", null=True, blank=True
    )
    payment_url = EncryptedTextField(
        purpose="billing.psp.payment_url", null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Retain one immutable fact per callback or imported statement identity."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("provider", "source", "event_id"), name="billing_psp_event_uniq"
            ),
        ]


class PspReceipt(TenantScopedModel):
    """A receipt exists only for one posted and exactly allocated PSP collection."""

    reference = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intent = models.OneToOneField(PspIntent, on_delete=models.PROTECT)
    settlement = models.OneToOneField(
        "billing.JournalSettlement", on_delete=models.PROTECT
    )
    allocation = models.OneToOneField(
        "billing.SettlementAllocation", on_delete=models.PROTECT
    )
    amount_minor = models.PositiveBigIntegerField()
    issued_at = models.DateTimeField(default=timezone.now, editable=False)
