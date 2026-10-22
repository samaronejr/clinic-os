"""Authorized operators still cannot probe unknown or foreign order selectors."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.current_context import require_permission
from apps.orders.models import ResultMatch, ResultQuarantine
from apps.orders.services import ingest_result

from identity.permission_support import permission_actor, permission_context
from orders.support import ordered, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("foreign", [False, True])
def test_operator_unknown_and_foreign_orders_have_the_same_unfiled_receipt(
    rbac_graph: RbacGraph,
    foreign: bool,
) -> None:
    world = ordered(rbac_graph)
    other = replace(
        rbac_graph,
        organization_a=rbac_graph.organization_b,
        clinic_a=rbac_graph.clinic_c,
    )
    actor, _assignment = permission_actor(other, "receptionist")
    with permission_context(other, actor):
        require_permission("order.route", clinic_id=other.clinic_a)
        receipt = ingest_result(
            clinic_id=other.clinic_a,
            submission=replace(
                submission(world),
                order_id=world.order.pk if foreign else uuid4(),
            ),
        )
        assert receipt.state == "quarantined"
        assert (
            ResultQuarantine.objects.get(version_id=receipt.version_id).reason
            == "unknown_order"
        )
        assert not ResultMatch.objects.filter(version_id=receipt.version_id).exists()
