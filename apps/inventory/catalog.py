"""Manager-published catalog and minimum stock thresholds."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError

from apps.inventory.access import INVALID, authority, execute, label, positive
from apps.inventory.models import Batch, Item, Location, MinimumStock, Supplier

if TYPE_CHECKING:
    from uuid import UUID

MAX_CONTACT = 1000


def create_item(
    *,
    clinic_id: UUID,
    name: str,
    unit: str,
    command_id: UUID,
) -> Item:
    """Publish an indivisible-unit consumable identity."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    label(name, maximum=160)
    label(unit, maximum=24)
    return execute(
        Item,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="create_item",
        permission="inventory.manage",
        payload={"name": name, "unit": unit},
        create=lambda _actor, tenant: Item.objects.create(
            organization_id=tenant, clinic_id=clinic_id, name=name, unit=unit
        ),
    )


def create_location(*, clinic_id: UUID, name: str, command_id: UUID) -> Location:
    """Publish a storage identity, not a patient location record."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    label(name, maximum=160)
    return execute(
        Location,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="create_location",
        permission="inventory.manage",
        payload={"name": name},
        create=lambda _actor, tenant: Location.objects.create(
            organization_id=tenant, clinic_id=clinic_id, name=name
        ),
    )


def create_supplier(
    *,
    clinic_id: UUID,
    name: str,
    contact: str,
    command_id: UUID,
) -> Supplier:
    """Protect supplier identity/contact in purpose-bound envelope fields."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    label(name, maximum=160)
    if not isinstance(contact, str) or len(contact) > MAX_CONTACT:
        raise ValidationError(INVALID)
    return execute(
        Supplier,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="create_supplier",
        permission="inventory.manage",
        payload={"name": name, "contact": contact},
        create=lambda _actor, tenant: Supplier.objects.create(
            organization_id=tenant, clinic_id=clinic_id, name=name, contact=contact
        ),
    )


def create_batch(
    *,
    clinic_id: UUID,
    item_id: UUID,
    lot: str,
    expires_on: date,
    command_id: UUID,
) -> Batch:
    """Publish a lot with an immutable expiry date."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    label(lot, maximum=120)
    if type(expires_on) is not date:
        raise ValidationError(INVALID)
    return execute(
        Batch,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="create_batch",
        permission="inventory.manage",
        payload={
            "item": str(item_id),
            "lot": lot,
            "expires_on": expires_on.isoformat(),
        },
        create=lambda _actor, tenant: Batch.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            item_id=item_id,
            lot=lot,
            expires_on=expires_on,
        ),
    )


def set_minimum_stock(
    *,
    clinic_id: UUID,
    item_id: UUID,
    location_id: UUID,
    quantity: int,
    command_id: UUID,
) -> MinimumStock:
    """Retain each threshold publication; alert queries select the newest one."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    positive(quantity)
    return execute(
        MinimumStock,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="set_minimum_stock",
        permission="inventory.manage",
        payload={
            "item": str(item_id),
            "location": str(location_id),
            "quantity": quantity,
        },
        create=lambda _actor, tenant: MinimumStock.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            item_id=item_id,
            location_id=location_id,
            quantity=quantity,
        ),
    )
