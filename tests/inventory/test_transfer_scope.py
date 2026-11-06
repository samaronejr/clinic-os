"""An explicit organizational grant authorizes both transfer endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.inventory.models import Location, StockMovement
from apps.inventory.services import balance, transfer, use

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from inventory.support import stocked

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_authorized_transfer_keeps_lot_and_destination_scope(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    receiver = User.objects.create(username=f"synthetic-inventory-{uuid4().hex}")
    with owner_context(rbac_graph.organization_a):
        for clinic in (rbac_graph.clinic_a, rbac_graph.clinic_b):
            UserClinicRole.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=clinic,
                user_id=world.manager,
                role="org_admin",
            )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=world.manager,
            role="clinic_manager",
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user=receiver,
            role="nurse",
        )
        destination = Location.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            name="Synthetic destination",
        )
    with as_actor(rbac_graph, world.manager):
        moved = transfer(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            source_location_id=world.location,
            destination_location_id=destination.pk,
            quantity=3,
            command_id=uuid4(),
        )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 2
        )
    with as_actor(rbac_graph, receiver.pk):
        assert StockMovement.objects.get(pk=moved.pk).batch_id == world.batch
        use(
            clinic_id=rbac_graph.clinic_b,
            batch_id=world.batch,
            location_id=destination.pk,
            quantity=1,
            command_id=uuid4(),
        )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_b,
                batch_id=world.batch,
                location_id=destination.pk,
            )
            == 2
        )
