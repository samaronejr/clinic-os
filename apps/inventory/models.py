"""Retained stock facts and approval-bound procurement; never mutable balances."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class ScopedRecord(TenantScopedModel):
    """Every inventory fact belongs to an explicit clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """The concrete records own their scope constraints and policies."""

        abstract = True


class CommandedRecord(ScopedRecord):
    """An immutable command identity, including for owner-provisioned fixtures."""

    command_id = models.UUIDField(default=uuid.uuid4)

    class Meta:
        """Never permit two effects of the same command in one table."""

        abstract = True
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "command_id"), name="%(class)s_inventory_command"
            )
        ]


class Supplier(ScopedRecord):
    """Supplier contact data is encrypted, not replicated into stock receipts."""

    name = EncryptedTextField(purpose="inventory.supplier.name")
    contact = EncryptedTextField(purpose="inventory.supplier.contact", null=True)


class Item(ScopedRecord):
    """A consumable catalog entry; unit is the indivisible accounting unit."""

    name = models.CharField(max_length=160)
    unit = models.CharField(max_length=24, default="each")


class Batch(ScopedRecord):
    """A retained lot identity and conservative local-date expiry boundary."""

    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    lot = models.CharField(max_length=120)
    expires_on = models.DateField()

    class Meta:
        """Lot identity cannot be silently redefined within an item."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "item", "lot"), name="inventory_batch_lot"
            )
        ]


class Location(ScopedRecord):
    """A physical storage location, never a patient reference."""

    name = models.CharField(max_length=160)


class MinimumStock(ScopedRecord):
    """An immutable item/location threshold; latest publication wins."""

    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()


class PurchaseBudget(CommandedRecord):
    """A manager-approved, non-reusable spending envelope in BRL centavos."""

    amount_minor = models.PositiveBigIntegerField()
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField()


class PurchaseRequest(CommandedRecord):
    """Only draft -> approved -> received can change this retained request."""

    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    budget = models.ForeignKey(PurchaseBudget, null=True, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    amount_minor = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16, default="draft")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True)
    last_command_id = models.UUIDField(null=True)


class StockMovement(CommandedRecord):
    """One immutable fact; a transfer contains its debit and credit atomically."""

    batch = models.ForeignKey(Batch, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    destination_location = models.ForeignKey(
        Location, null=True, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=16)
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=24, default="", blank=True)
    source_movement = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
    procedure_entry = models.ForeignKey(
        "ehr.SpecialtyEntry", null=True, on_delete=models.PROTECT
    )
    purchase_request = models.OneToOneField(
        PurchaseRequest, null=True, on_delete=models.PROTECT
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )


class Equipment(CommandedRecord):
    """An asset identity with retained calibration and maintenance evidence."""

    name = models.CharField(max_length=160)
    serial = models.CharField(max_length=120)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)


class Calibration(CommandedRecord):
    """An append-only calibration result, not a mutable equipment flag."""

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT)
    performed_on = models.DateField()
    due_on = models.DateField()
    outcome = models.CharField(max_length=16)


class Maintenance(CommandedRecord):
    """A maintenance receipt cannot erase a failed calibration."""

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT)
    performed_on = models.DateField()
    due_on = models.DateField()
    outcome = models.CharField(max_length=16)


class CommandReceipt(ScopedRecord):
    """One closed operation/payload binding across all inventory services."""

    command_id = models.UUIDField()
    operation = models.CharField(max_length=32)
    fingerprint = models.CharField(max_length=64)
    object_type = models.CharField(max_length=24)
    object_id = models.UUIDField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        """Commands cannot be replayed as a different inventory operation."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "command_id"), name="inventory_receipt_command"
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="inventory_receipt_digest",
            ),
        ]
