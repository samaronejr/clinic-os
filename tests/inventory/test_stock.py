"""Movements are append-only facts; balances cannot become negative."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Literal, assert_never
from uuid import uuid4

import pytest
from apps.inventory.models import StockMovement
from apps.inventory.services import (
    InventoryConflictError,
    balance,
    receive,
    return_stock,
    transfer,
    use,
)
from django.core.exceptions import ValidationError
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from inventory.support import stocked

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_two_competing_uses_commit_only_one_debit(rbac_graph: RbacGraph) -> None:
    # Given: two independently committed sessions compete for the final units.
    world = stocked(rbac_graph)
    start = Barrier(2, timeout=15)

    def consume() -> str:
        close_old_connections()
        try:
            start.wait()
            with as_actor(rbac_graph, world.nurse):
                use(
                    clinic_id=rbac_graph.clinic_a,
                    batch_id=world.batch,
                    location_id=world.location,
                    quantity=4,
                    command_id=uuid4(),
                )
        except InventoryConflictError as error:
            return str(error)
        else:
            return "committed"
        finally:
            connections.close_all()

    # When: both commands are released together; no sleep or polling is used.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(consume), pool.submit(consume)
        outcomes = [first.result(timeout=30), second.result(timeout=30)]

    # Then: denial is the stock conflict, not a swallowed DB/worker failure.
    assert sorted(outcomes) == ["committed", "insufficient_stock"]
    with as_actor(rbac_graph, world.manager):
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 1
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.filter(kind="use").count() == 1


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_invalid_quantity_creates_no_movement(
    rbac_graph: RbacGraph, quantity: int
) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.nurse), pytest.raises(ValidationError):
        use(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=quantity,
            command_id=uuid4(),
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.count() == 1


def test_receipt_replay_has_one_effect_and_changed_payload_is_refused(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    command = uuid4()
    with as_actor(rbac_graph, world.manager):
        first = receive(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=2,
            command_id=command,
        )
        replay = receive(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=2,
            command_id=command,
        )
        assert first.pk == replay.pk
        with pytest.raises(InventoryConflictError, match="idempotency_conflict"):
            receive(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
                quantity=3,
                command_id=command,
            )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 7
        )


@pytest.mark.parametrize("mutation", ["update", "delete", "truncate"])
def test_runtime_cannot_rewrite_movement_history(
    rbac_graph: RbacGraph, mutation: Literal["update", "delete", "truncate"]
) -> None:
    world = stocked(rbac_graph)

    def mutate() -> None:
        match mutation:
            case "update":
                StockMovement.objects.filter(batch_id=world.batch).update(quantity=99)
            case "delete":
                StockMovement.objects.filter(batch_id=world.batch).delete()
            case "truncate":
                with connection.cursor() as cursor:
                    cursor.execute("TRUNCATE clinic_app.inventory_stockmovement")
            case unreachable:
                assert_never(unreachable)

    with as_actor(rbac_graph, world.manager):
        with pytest.raises(DatabaseError), transaction.atomic():
            mutate()
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 5
        )


def test_return_is_linked_to_use_and_cannot_exceed_original_debit(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.nurse):
        debit = use(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=3,
            command_id=uuid4(),
        )
        returned = return_stock(
            clinic_id=rbac_graph.clinic_a,
            use_movement_id=debit.pk,
            quantity=2,
            command_id=uuid4(),
        )
        assert returned.source_movement_id == debit.pk
        with pytest.raises(InventoryConflictError, match="return_exceeds_use"):
            return_stock(
                clinic_id=rbac_graph.clinic_a,
                use_movement_id=debit.pk,
                quantity=2,
                command_id=uuid4(),
            )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 4
        )


def test_transfer_changes_both_location_balances_atomically(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.manager):
        transfer(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            source_location_id=world.location,
            destination_location_id=world.other_location,
            quantity=3,
            command_id=uuid4(),
        )
        assert [
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=location,
            )
            for location in (world.location, world.other_location)
        ] == [2, 3]


def test_direct_runtime_debit_cannot_bypass_nonnegative_stock(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.nurse):
        with pytest.raises(DatabaseError), transaction.atomic():
            StockMovement.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                kind="use",
                batch_id=world.batch,
                location_id=world.location,
                quantity=6,
                command_id=uuid4(),
                recorded_by_id=world.nurse,
            )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 5
        )
