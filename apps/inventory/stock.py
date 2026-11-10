"""Append stock facts and query derived balances; no treatment recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection

from apps.inventory.access import (
    InventoryAccessDeniedError,
    authority,
    execute,
    positive,
    record,
    today,
)
from apps.inventory.models import Batch, Location, MinimumStock, StockMovement

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

EXPIRY_ALERT_DAYS = 30


@dataclass(frozen=True, slots=True)
class StockAlert:
    """Selectors and machine status only; no clinical or supplier content."""

    kind: str
    item_id: UUID
    location_id: UUID
    batch_id: UUID | None = None


def balance(*, clinic_id: UUID, batch_id: UUID, location_id: UUID) -> int:
    """Return the movement sum for an authorized batch/location pair."""
    authority(clinic_id=clinic_id, permission="inventory.read")
    record(Location, clinic_id=clinic_id, record_id=location_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.inventory_batch_allowed(%s,%s)",
            [batch_id, clinic_id],
        )
        if cursor.fetchone() != (True,):
            raise InventoryAccessDeniedError
        cursor.execute(
            "SELECT clinic_app.inventory_balance(%s,%s)", [batch_id, location_id]
        )
        row = cursor.fetchone()
    if row is None:
        raise InventoryAccessDeniedError
    authority(clinic_id=clinic_id, permission="inventory.read")
    return int(row[0])


def movement(  # noqa: PLR0913 - one closed stock command
    *,
    clinic_id: UUID,
    batch_id: UUID,
    location_id: UUID,
    quantity: int,
    command_id: UUID,
    kind: str,
    operation: str,
    destination_location_id: UUID | None = None,
    source_movement_id: UUID | None = None,
    procedure_entry_id: UUID | None = None,
    purchase_request_id: UUID | None = None,
    reason: str = "",
) -> StockMovement:
    """DB guards decide scope, stock, expiry and transfer atomically."""
    permission = "inventory.use" if kind in {"use", "return"} else "inventory.manage"
    authority(clinic_id=clinic_id, permission=permission)
    positive(quantity)

    def create(actor: UUID, tenant: UUID) -> StockMovement:
        return StockMovement.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            batch_id=batch_id,
            location_id=location_id,
            quantity=quantity,
            command_id=command_id,
            kind=kind,
            destination_location_id=destination_location_id,
            source_movement_id=source_movement_id,
            procedure_entry_id=procedure_entry_id,
            purchase_request_id=purchase_request_id,
            reason=reason,
            recorded_by_id=actor,
        )

    return execute(
        StockMovement,
        clinic_id=clinic_id,
        command_id=command_id,
        operation=operation,
        permission=permission,
        payload={
            "batch": str(batch_id),
            "location": str(location_id),
            "quantity": quantity,
            "destination": str(destination_location_id),
            "source": str(source_movement_id),
            "procedure": str(procedure_entry_id),
            "purchase": str(purchase_request_id),
            "reason": reason,
        },
        create=create,
    )


def receive(
    *,
    clinic_id: UUID,
    batch_id: UUID,
    location_id: UUID,
    quantity: int,
    command_id: UUID,
) -> StockMovement:
    """Record physical receipt, including expired stock awaiting disposal."""
    return movement(
        clinic_id=clinic_id,
        batch_id=batch_id,
        location_id=location_id,
        quantity=quantity,
        command_id=command_id,
        kind="receipt",
        operation="receive",
    )


def use(  # noqa: PLR0913 - pinned clinic-scoped service contract
    *,
    clinic_id: UUID,
    batch_id: UUID,
    location_id: UUID,
    quantity: int,
    command_id: UUID,
    procedure_entry_id: UUID | None = None,
) -> StockMovement:
    """Consume units, optionally binding the exact retained procedure entry."""
    return movement(
        clinic_id=clinic_id,
        batch_id=batch_id,
        location_id=location_id,
        quantity=quantity,
        command_id=command_id,
        kind="use",
        operation="use",
        procedure_entry_id=procedure_entry_id,
    )


def return_stock(
    *,
    clinic_id: UUID,
    use_movement_id: UUID,
    quantity: int,
    command_id: UUID,
) -> StockMovement:
    """Return only previously used units to their original lot and location."""
    authority(clinic_id=clinic_id, permission="inventory.use")
    source = record(StockMovement, clinic_id=clinic_id, record_id=use_movement_id)
    return movement(
        clinic_id=clinic_id,
        batch_id=source.batch_id,
        location_id=source.location_id,
        quantity=quantity,
        command_id=command_id,
        kind="return",
        operation="return_stock",
        source_movement_id=source.pk,
    )


def write_off(  # noqa: PLR0913 - pinned clinic-scoped service contract
    *,
    clinic_id: UUID,
    batch_id: UUID,
    location_id: UUID,
    quantity: int,
    reason: str,
    command_id: UUID,
) -> StockMovement:
    """Dispose of stock with manager authority and a nonclinical reason code."""
    return movement(
        clinic_id=clinic_id,
        batch_id=batch_id,
        location_id=location_id,
        quantity=quantity,
        command_id=command_id,
        kind="write_off",
        operation="write_off",
        reason=reason,
    )


def transfer(  # noqa: PLR0913 - pinned clinic-scoped service contract
    *,
    clinic_id: UUID,
    batch_id: UUID,
    source_location_id: UUID,
    destination_location_id: UUID,
    quantity: int,
    command_id: UUID,
) -> StockMovement:
    """One immutable transfer fact changes both location sums atomically."""
    return movement(
        clinic_id=clinic_id,
        batch_id=batch_id,
        location_id=source_location_id,
        quantity=quantity,
        command_id=command_id,
        kind="transfer",
        operation="transfer",
        destination_location_id=destination_location_id,
    )


def stock_alerts(*, clinic_id: UUID) -> tuple[StockAlert, ...]:
    """Report expired/expiring positive stock and latest location thresholds."""
    authority(clinic_id=clinic_id, permission="inventory.read")
    checked_on: date = today(clinic_id=clinic_id)
    alerts = []
    locations = tuple(Location.objects.filter(clinic_id=clinic_id).order_by("pk"))
    batches = tuple(Batch.objects.order_by("pk"))
    totals: dict[tuple[UUID, UUID], int] = {}
    for location in locations:
        for batch in batches:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.inventory_batch_allowed(%s,%s)",
                    [batch.pk, clinic_id],
                )
                if cursor.fetchone() != (True,):
                    continue
            units = balance(
                clinic_id=clinic_id, batch_id=batch.pk, location_id=location.pk
            )
            key = (batch.item_id, location.pk)
            if batch.expires_on > checked_on:
                totals[key] = totals.get(key, 0) + units
            days = (batch.expires_on - checked_on).days
            if units > 0 and days <= EXPIRY_ALERT_DAYS:
                alerts.append(
                    StockAlert(
                        "expired" if days <= 0 else "expiring",
                        batch.item_id,
                        location.pk,
                        batch.pk,
                    )
                )
    seen = set()
    for minimum in MinimumStock.objects.filter(clinic_id=clinic_id).order_by(
        "-created_at", "-pk"
    ):
        key = (minimum.item_id, minimum.location_id)
        if key not in seen and totals.get(key, 0) < minimum.quantity:
            alerts.append(StockAlert("below_minimum", *key))
        seen.add(key)
    authority(clinic_id=clinic_id, permission="inventory.read")
    return tuple(alerts)
