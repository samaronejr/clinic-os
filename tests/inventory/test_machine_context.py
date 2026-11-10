"""An active machine context is refused on every Inventory path.

Staff guards refuse a machine context even beside a staff actor GUC
(apps/identity/README.md, service principals). The shared permission
evaluator reads only the actor and tenant, so Inventory refuses
app.current_principal itself: Python through current_context, SQL through
NO_MACHINE_CONTEXT in its resolvers, guard trigger and policies. Each SQL case
first succeeds for the authorized staff session, then repeats it with a
machine principal added.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.inventory import services
from apps.inventory.migrations._inventory_sql import TABLES
from apps.inventory.models import (
    Batch,
    Calibration,
    CommandReceipt,
    Equipment,
    Item,
    Location,
    Maintenance,
    MinimumStock,
    PurchaseBudget,
    PurchaseRequest,
    StockMovement,
    Supplier,
)
from django.apps import apps as django_apps
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from inventory.support import stocked

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.models import Model

    from inventory.support import StockWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
# Any non-empty value is an active machine context, registered or forged.
PRINCIPALS: Final = (str(UUID(int=610061)), "malformed-principal")
PAST: Final = date(2000, 1, 1)
FUTURE: Final = date(2999, 1, 1)
MODELS: Final[dict[str, type[Model]]] = {
    model._meta.db_table: model
    for model in django_apps.get_app_config("inventory").get_models()
}


@dataclass(frozen=True, slots=True)
class Scope:
    organization_id: UUID
    clinic_id: UUID
    world: StockWorld
    equipment: UUID

    def ids(self) -> dict[str, UUID]:
        return {"organization_id": self.organization_id, "clinic_id": self.clinic_id}


# One valid row per table, written as the authorized manager's own session.
BUILDERS: Final[dict[str, Callable[[Scope], object]]] = {
    "inventory_supplier": lambda s: Supplier.objects.create(
        **s.ids(), name="Synthetic supplier"
    ),
    "inventory_item": lambda s: Item.objects.create(**s.ids(), name="Synthetic swab"),
    "inventory_batch": lambda s: Batch.objects.create(
        **s.ids(), item_id=s.world.item, lot=uuid4().hex, expires_on=FUTURE
    ),
    "inventory_location": lambda s: Location.objects.create(
        **s.ids(), name="Synthetic shelf"
    ),
    "inventory_minimumstock": lambda s: MinimumStock.objects.create(
        **s.ids(), item_id=s.world.item, location_id=s.world.location, quantity=1
    ),
    "inventory_purchasebudget": lambda s: PurchaseBudget.objects.create(
        **s.ids(),
        amount_minor=100,
        approved_by_id=s.world.manager,
        approved_at=timezone.now(),
    ),
    "inventory_purchaserequest": lambda s: PurchaseRequest.objects.create(
        **s.ids(),
        item_id=s.world.item,
        supplier_id=s.world.supplier,
        quantity=1,
        amount_minor=100,
    ),
    "inventory_stockmovement": lambda s: StockMovement.objects.create(
        **s.ids(),
        kind="receipt",
        batch_id=s.world.batch,
        location_id=s.world.location,
        quantity=1,
        recorded_by_id=s.world.manager,
    ),
    "inventory_equipment": lambda s: Equipment.objects.create(
        **s.ids(),
        supplier_id=s.world.supplier,
        location_id=s.world.location,
        name="Synthetic scale",
        serial=uuid4().hex,
    ),
    "inventory_calibration": lambda s: Calibration.objects.create(
        **s.ids(),
        equipment_id=s.equipment,
        performed_on=PAST,
        due_on=FUTURE,
        outcome="passed",
    ),
    "inventory_maintenance": lambda s: Maintenance.objects.create(
        **s.ids(),
        equipment_id=s.equipment,
        performed_on=PAST,
        due_on=FUTURE,
        outcome="completed",
    ),
    "inventory_commandreceipt": lambda s: CommandReceipt.objects.create(
        **s.ids(),
        command_id=uuid4(),
        operation="create_item",
        fingerprint="0" * 64,
        object_type="item",
        object_id=s.world.item,
        recorded_by_id=s.world.manager,
    ),
}


def scope(graph: RbacGraph) -> Scope:
    world = stocked(graph)
    with owner_context(graph.organization_a):
        equipment = Equipment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            supplier_id=world.supplier,
            location_id=world.location,
            name="Synthetic autoclave",
            serial="SYNTHETIC-61",
        )
    return Scope(graph.organization_a, graph.clinic_a, world, equipment.pk)


@contextmanager
def session(graph: RbacGraph, actor: UUID, principal: str = "") -> Iterator[None]:
    """The authorized staff session; a non-empty principal adds machine context."""
    with as_actor(graph, actor), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_principal',%s,true)", [principal]
        )
        yield


@contextmanager
def refused() -> Iterator[None]:
    """The statement fails closed with an authorization SQLSTATE."""
    with pytest.raises(DatabaseError) as error, transaction.atomic():
        yield
    assert getattr(error.value.__cause__, "sqlstate", None) == "42501"


def _rows(model: type[Model]) -> int:
    return model._default_manager.count()


def _refuses(call: Callable[[], object]) -> bool:
    try:
        call()
    except services.InventoryAccessDeniedError:
        return True
    return False


def test_oracle_covers_every_inventory_table() -> None:
    assert set(BUILDERS) == set(MODELS) == set(TABLES)


@pytest.mark.parametrize("table", sorted(TABLES))
def test_every_table_hides_rows_from_a_machine_context(
    rbac_graph: RbacGraph, table: str
) -> None:
    current = scope(rbac_graph)
    model = MODELS[table]
    with session(rbac_graph, current.world.manager):
        BUILDERS[table](current)
        assert model._default_manager.exists()
    with session(rbac_graph, current.world.manager, PRINCIPALS[0]):
        assert not model._default_manager.exists()


@pytest.mark.parametrize("table", sorted(TABLES))
def test_every_table_refuses_machine_writes(rbac_graph: RbacGraph, table: str) -> None:
    current = scope(rbac_graph)
    model = MODELS[table]
    with session(rbac_graph, current.world.manager):
        BUILDERS[table](current)
    with owner_context(rbac_graph.organization_a):
        written = _rows(model)
    with session(rbac_graph, current.world.manager, PRINCIPALS[0]), refused():
        BUILDERS[table](current)
    with owner_context(rbac_graph.organization_a):
        assert _rows(model) == written


@pytest.mark.parametrize("principal", PRINCIPALS)
def test_sql_resolvers_refuse_a_machine_context(
    rbac_graph: RbacGraph, principal: str
) -> None:
    world = stocked(rbac_graph)
    clinic = rbac_graph.clinic_a
    read = (
        "SELECT clinic_app.inventory_today(%s),"
        "clinic_app.inventory_batch_allowed(%s,%s)"
    )
    balance = "SELECT clinic_app.inventory_balance(%s,%s)"
    with session(rbac_graph, world.manager), connection.cursor() as cursor:
        cursor.execute(read, [clinic, world.batch, clinic])
        row = cursor.fetchone()
        assert row is not None
        assert isinstance(row[0], date)
        assert row[1] is True
        cursor.execute(balance, [world.batch, world.location])
        assert cursor.fetchone() == (5,)
    with session(rbac_graph, world.manager, principal), connection.cursor() as cursor:
        cursor.execute(read, [clinic, world.batch, clinic])
        assert cursor.fetchone() == (None, False)
        with refused():
            cursor.execute(balance, [world.batch, world.location])


def test_transferred_stock_is_hidden_from_a_machine_context(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    receiver = User.objects.create(username=f"synthetic-inventory-{uuid4().hex}")
    with owner_context(rbac_graph.organization_a):
        for clinic, role, user in (
            (rbac_graph.clinic_a, "org_admin", world.manager),
            (rbac_graph.clinic_b, "org_admin", world.manager),
            (rbac_graph.clinic_b, "clinic_manager", world.manager),
            (rbac_graph.clinic_b, "nurse", receiver.pk),
        ):
            UserClinicRole.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=clinic,
                user_id=user,
                role=role,
            )
        destination = Location.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            name="Synthetic destination",
        )
    with as_actor(rbac_graph, world.manager):
        moved = services.transfer(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            source_location_id=world.location,
            destination_location_id=destination.pk,
            quantity=3,
            command_id=uuid4(),
        )
    # The destination reads the lot and its incoming fact only through the
    # transferred-stock branches of the batch and movement read policies.
    for principal, visible in (("", True), (PRINCIPALS[0], False)):
        with session(rbac_graph, receiver.pk, principal):
            assert Batch.objects.filter(pk=world.batch).exists() is visible
            assert StockMovement.objects.filter(pk=moved.pk).exists() is visible
    with session(rbac_graph, world.manager, PRINCIPALS[0]), refused():
        StockMovement.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            kind="transfer",
            batch_id=world.batch,
            location_id=world.location,
            destination_location_id=destination.pk,
            quantity=1,
            recorded_by_id=world.manager,
        )


def test_machine_context_cannot_advance_a_purchase(rbac_graph: RbacGraph) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.nurse):
        request = services.create_purchase_request(
            clinic_id=rbac_graph.clinic_a,
            item_id=world.item,
            supplier_id=world.supplier,
            quantity=1,
            amount_minor=100,
            command_id=uuid4(),
        )
    approval: dict[str, object] = {
        "state": "approved",
        "approved_by_id": world.manager,
        "last_command_id": uuid4(),
    }
    for principal, updated in ((PRINCIPALS[0], 0), ("", 1)):
        with session(rbac_graph, world.manager, principal):
            rows = PurchaseRequest.objects.filter(pk=request.pk).update(**approval)
            assert rows == updated


def test_every_inventory_service_refuses_a_machine_context(
    rbac_graph: RbacGraph,
) -> None:
    current = scope(rbac_graph)
    world, clinic = current.world, rbac_graph.clinic_a
    purchase = {
        "clinic_id": clinic,
        "item_id": world.item,
        "supplier_id": world.supplier,
    }
    stock = {
        "clinic_id": clinic,
        "batch_id": world.batch,
        "location_id": world.location,
    }
    with as_actor(rbac_graph, world.manager):
        budget = services.approve_budget(
            clinic_id=clinic, amount_minor=1000, command_id=uuid4()
        )
        draft = services.create_purchase_request(
            **purchase, quantity=1, amount_minor=100, command_id=uuid4()
        )
        approved = services.create_purchase_request(
            **purchase, quantity=1, amount_minor=100, command_id=uuid4()
        )
        services.approve_purchase(
            clinic_id=clinic, purchase_request_id=approved.pk, command_id=uuid4()
        )
        used = services.use(**stock, quantity=1, command_id=uuid4())
    calls: dict[str, Callable[[], object]] = {
        "approve_budget": lambda: services.approve_budget(
            clinic_id=clinic, amount_minor=100, command_id=uuid4()
        ),
        "approve_purchase": lambda: services.approve_purchase(
            clinic_id=clinic, purchase_request_id=draft.pk, command_id=uuid4()
        ),
        "balance": lambda: services.balance(**stock),
        "create_batch": lambda: services.create_batch(
            clinic_id=clinic,
            item_id=world.item,
            lot="SYNTHETIC-MACHINE",
            expires_on=FUTURE,
            command_id=uuid4(),
        ),
        "create_item": lambda: services.create_item(
            clinic_id=clinic, name="Synthetic swab", unit="each", command_id=uuid4()
        ),
        "create_location": lambda: services.create_location(
            clinic_id=clinic, name="Synthetic shelf", command_id=uuid4()
        ),
        "create_purchase_request": lambda: services.create_purchase_request(
            **purchase, quantity=1, amount_minor=100, command_id=uuid4()
        ),
        "create_supplier": lambda: services.create_supplier(
            clinic_id=clinic, name="Synthetic supplier", contact="", command_id=uuid4()
        ),
        "equipment_status": lambda: services.equipment_status(
            clinic_id=clinic, equipment_id=current.equipment
        ),
        "propose_replenishment": lambda: services.propose_replenishment(
            **purchase,
            budget_id=budget.pk,
            quantity=1,
            amount_minor=100,
            command_id=uuid4(),
        ),
        "receive": lambda: services.receive(**stock, quantity=1, command_id=uuid4()),
        "receive_purchase": lambda: services.receive_purchase(
            **stock, purchase_request_id=approved.pk, command_id=uuid4()
        ),
        "record_calibration": lambda: services.record_calibration(
            clinic_id=clinic,
            equipment_id=current.equipment,
            performed_on=PAST,
            due_on=FUTURE,
            outcome="passed",
            command_id=uuid4(),
        ),
        "record_maintenance": lambda: services.record_maintenance(
            clinic_id=clinic,
            equipment_id=current.equipment,
            performed_on=PAST,
            due_on=FUTURE,
            outcome="completed",
            command_id=uuid4(),
        ),
        "register_equipment": lambda: services.register_equipment(
            clinic_id=clinic,
            supplier_id=world.supplier,
            location_id=world.location,
            name="Synthetic scale",
            serial="SYNTHETIC-MACHINE",
            command_id=uuid4(),
        ),
        "return_stock": lambda: services.return_stock(
            clinic_id=clinic, use_movement_id=used.pk, quantity=1, command_id=uuid4()
        ),
        "set_minimum_stock": lambda: services.set_minimum_stock(
            clinic_id=clinic,
            item_id=world.item,
            location_id=world.location,
            quantity=1,
            command_id=uuid4(),
        ),
        "stock_alerts": lambda: services.stock_alerts(clinic_id=clinic),
        "transfer": lambda: services.transfer(
            clinic_id=clinic,
            batch_id=world.batch,
            source_location_id=world.location,
            destination_location_id=world.other_location,
            quantity=1,
            command_id=uuid4(),
        ),
        "use": lambda: services.use(**stock, quantity=1, command_id=uuid4()),
        "write_off": lambda: services.write_off(
            **stock, quantity=1, reason="damaged", command_id=uuid4()
        ),
    }
    assert set(calls) == {name for name in services.__all__ if name[0].islower()}
    with owner_context(rbac_graph.organization_a):
        before = {table: _rows(model) for table, model in MODELS.items()}
    with session(rbac_graph, world.manager, PRINCIPALS[0]):
        allowed = [name for name, call in calls.items() if not _refuses(call)]
    assert allowed == []
    with owner_context(rbac_graph.organization_a):
        assert {table: _rows(model) for table, model in MODELS.items()} == before
