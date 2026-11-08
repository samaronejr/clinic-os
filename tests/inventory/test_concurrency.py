"""Budget and cumulative-return races use independent committed transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.inventory import services
from django.db import close_old_connections, connections

from ehr.test_autosave import as_actor
from inventory.support import stocked

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_competing_returns_cannot_exceed_the_original_use(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.nurse):
        debit = services.use(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=3,
            command_id=uuid4(),
        )
    start = Barrier(2, timeout=15)

    def give_back() -> str:
        close_old_connections()
        try:
            start.wait()
            with as_actor(rbac_graph, world.nurse):
                services.return_stock(
                    clinic_id=rbac_graph.clinic_a,
                    use_movement_id=debit.pk,
                    quantity=2,
                    command_id=uuid4(),
                )
        except services.InventoryConflictError as error:
            return str(error)
        else:
            return "committed"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(give_back) for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert sorted(outcomes) == ["committed", "return_exceeds_use"]
    with as_actor(rbac_graph, world.nurse):
        assert (
            services.balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 4
        )


def test_budget_approval_reservation_is_atomic_under_competing_requests(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with as_actor(rbac_graph, world.manager):
        budget = services.approve_budget(
            clinic_id=rbac_graph.clinic_a, amount_minor=1000, command_id=uuid4()
        )
        drafts = [
            services.propose_replenishment(
                clinic_id=rbac_graph.clinic_a,
                budget_id=budget.pk,
                item_id=world.item,
                supplier_id=world.supplier,
                quantity=5,
                amount_minor=600,
                command_id=uuid4(),
            ).pk
            for _ in range(2)
        ]
    start = Barrier(2, timeout=15)

    def approve(request_id: UUID) -> str:
        close_old_connections()
        try:
            start.wait()
            with as_actor(rbac_graph, world.manager):
                services.approve_purchase(
                    clinic_id=rbac_graph.clinic_a,
                    purchase_request_id=request_id,
                    command_id=uuid4(),
                )
        except services.InventoryConflictError as error:
            return str(error)
        else:
            return "committed"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, request_id) for request_id in drafts]
        outcomes = [future.result(timeout=30) for future in futures]
    assert sorted(outcomes) == ["budget_exceeded", "committed"]
