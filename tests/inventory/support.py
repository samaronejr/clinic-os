"""Synthetic inventory fixtures; no stock or authorization behavior is mocked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.inventory.models import Batch, Item, Location, Supplier
from apps.inventory.services import receive

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_actor

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class StockWorld:
    manager: UUID
    nurse: UUID
    item: UUID
    batch: UUID
    location: UUID
    other_location: UUID
    supplier: UUID


def stock_world(graph: RbacGraph, *, expired: bool = False) -> StockWorld:
    manager, _ = permission_actor(graph, "clinic_manager")
    nurse, _ = permission_actor(graph, "nurse")
    with owner_context(graph.organization_a):
        supplier = Supplier.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name="Synthetic supplier",
        )
        item = Item.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name="Synthetic gauze",
            unit="each",
        )
        batch = Batch.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            item=item,
            lot="SYNTHETIC-61",
            expires_on=date(2000, 1, 1) if expired else date(2999, 1, 1),
        )
        locations = tuple(
            Location.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                name=name,
            ).pk
            for name in ("Synthetic store", "Synthetic procedure room")
        )
    return StockWorld(
        manager, nurse, item.pk, batch.pk, locations[0], locations[1], supplier.pk
    )


def stocked(graph: RbacGraph, *, quantity: int = 5) -> StockWorld:
    world = stock_world(graph)
    with as_actor(graph, world.manager):
        receive(
            clinic_id=graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=quantity,
            command_id=uuid4(),
        )
    return world
