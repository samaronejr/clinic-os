"""Collection state has exact replay and never fabricates clinical review."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.orders.models import ResultAcknowledgment, ResultRelease
from apps.orders.services import OrdersConflictError, progress_order

from ehr.test_autosave import as_actor
from orders.support import ordered

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_order_progression_is_replayed_once_and_cannot_reverse(
    rbac_graph: RbacGraph,
) -> None:
    world, command = ordered(rbac_graph), uuid4()
    with as_actor(rbac_graph, rbac_graph.physician):
        collected = progress_order(
            clinic_id=rbac_graph.clinic_a,
            order_id=world.order.pk,
            expected_sequence=2,
            state="collected",
            command_id=command,
        )
        replay = progress_order(
            clinic_id=rbac_graph.clinic_a,
            order_id=world.order.pk,
            expected_sequence=2,
            state="collected",
            command_id=command,
        )
        assert replay.pk == collected.pk
        in_process = progress_order(
            clinic_id=rbac_graph.clinic_a,
            order_id=world.order.pk,
            expected_sequence=3,
            state="in_process",
            command_id=uuid4(),
        )
        assert in_process.sequence == 4
        with pytest.raises(OrdersConflictError, match="illegal_transition"):
            progress_order(
                clinic_id=rbac_graph.clinic_a,
                order_id=world.order.pk,
                expected_sequence=4,
                state="placed",
                command_id=uuid4(),
            )
        assert not ResultAcknowledgment.objects.exists()
        assert not ResultRelease.objects.exists()
