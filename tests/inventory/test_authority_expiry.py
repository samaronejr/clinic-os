"""Real role denials, expired material refusal, and retained disposal facts."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import RoleGrant, UserClinicRole
from apps.inventory.models import Batch, Location, StockMovement
from apps.inventory.services import (
    InventoryAccessDeniedError,
    InventoryConflictError,
    balance,
    receive,
    stock_alerts,
    transfer,
    use,
    write_off,
)
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ehr.test_autosave import as_actor
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from inventory.support import stock_world, stocked

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_expired_batch_is_alerted_and_cannot_be_used(rbac_graph: RbacGraph) -> None:
    world = stock_world(rbac_graph, expired=True)
    with as_actor(rbac_graph, world.manager):
        receive(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=5,
            command_id=uuid4(),
        )
        alerts = stock_alerts(clinic_id=rbac_graph.clinic_a)
        assert (world.batch, "expired") in {
            (alert.batch_id, alert.kind) for alert in alerts
        }
    with as_actor(rbac_graph, world.nurse):
        with pytest.raises(InventoryConflictError, match="batch_expired"):
            use(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
                quantity=1,
                command_id=uuid4(),
            )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 5
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.count() == 1


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_write_off_requires_inventory_manager_authority(
    rbac_graph: RbacGraph, role: str
) -> None:
    world = stocked(rbac_graph)
    actor, _ = permission_actor(rbac_graph, role)
    with as_actor(rbac_graph, actor):
        if role in {"clinic_manager", "clinic_admin"}:
            movement = write_off(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
                quantity=1,
                reason="damaged",
                command_id=uuid4(),
            )
            assert movement.kind == "write_off"
        else:
            with pytest.raises(InventoryAccessDeniedError):
                write_off(
                    clinic_id=rbac_graph.clinic_a,
                    batch_id=world.batch,
                    location_id=world.location,
                    quantity=1,
                    reason="damaged",
                    command_id=uuid4(),
                )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.filter(kind="write_off").count() == (
            1 if role in {"clinic_manager", "clinic_admin"} else 0
        )


@pytest.mark.parametrize("destination", ["same_org", "foreign_org", "unknown"])
def test_cross_clinic_transfer_without_org_permission_is_side_effect_free(
    rbac_graph: RbacGraph, destination: str
) -> None:
    world = stocked(rbac_graph)
    target = uuid4()
    if destination != "unknown":
        organization, clinic = (
            (rbac_graph.organization_a, rbac_graph.clinic_b)
            if destination == "same_org"
            else (rbac_graph.organization_b, rbac_graph.clinic_c)
        )
        with owner_context(organization):
            target = Location.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                name="Synthetic other clinic",
            ).pk
    with as_actor(rbac_graph, world.manager):
        with pytest.raises(InventoryAccessDeniedError):
            transfer(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                source_location_id=world.location,
                destination_location_id=target,
                quantity=2,
                command_id=uuid4(),
            )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 5
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.count() == 1


def test_revoked_manager_cannot_dispose_stock(rbac_graph: RbacGraph) -> None:
    world = stocked(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=world.manager, clinic_id=rbac_graph.clinic_a
        ).update(revoked_at=timezone.now())
    with (
        permission_context(rbac_graph, world.manager),
        pytest.raises(InventoryAccessDeniedError),
    ):
        write_off(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=1,
            reason="damaged",
            command_id=uuid4(),
        )


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_use_is_limited_to_nurse_and_inventory_managers(
    rbac_graph: RbacGraph, role: str
) -> None:
    world = stocked(rbac_graph)
    actor, _ = permission_actor(rbac_graph, role)
    with as_actor(rbac_graph, actor):
        if role in {"nurse", "clinic_manager", "clinic_admin"}:
            movement = use(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
                quantity=1,
                command_id=uuid4(),
            )
            assert movement.kind == "use"
        else:
            with pytest.raises(InventoryAccessDeniedError):
                use(
                    clinic_id=rbac_graph.clinic_a,
                    batch_id=world.batch,
                    location_id=world.location,
                    quantity=1,
                    command_id=uuid4(),
                )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.filter(kind="use").count() == (
            1 if role in {"nurse", "clinic_manager", "clinic_admin"} else 0
        )


def test_narrowed_inventory_permission_is_checked_on_each_use(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="nurse",
            permission="inventory.use",
            bundle_version=2,
            valid_from=timezone.now(),
        )
    with (
        as_actor(rbac_graph, world.nurse),
        pytest.raises(InventoryAccessDeniedError),
    ):
        use(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=1,
            command_id=uuid4(),
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.count() == 1


def test_direct_write_off_cannot_bypass_nurse_authority(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph)
    with (
        as_actor(rbac_graph, world.nurse),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        StockMovement.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            kind="write_off",
            batch_id=world.batch,
            location_id=world.location,
            quantity=1,
            reason="damaged",
            command_id=uuid4(),
            recorded_by_id=world.nurse,
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.count() == 1


def test_expiry_boundary_uses_the_clinic_local_database_date(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.manager), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.inventory_today(%s),"
            "(statement_timestamp() AT TIME ZONE 'America/Sao_Paulo')::date",
            [rbac_graph.clinic_a],
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == row[1]
        local_date = row[0]
    with owner_context(rbac_graph.organization_a):
        batches = [
            Batch.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                item_id=world.item,
                lot=f"SYNTHETIC-BOUNDARY-{offset}",
                expires_on=local_date + timedelta(days=offset),
            )
            for offset in (0, 1)
        ]
    with as_actor(rbac_graph, world.manager):
        for batch in batches:
            receive(
                clinic_id=rbac_graph.clinic_a,
                batch_id=batch.pk,
                location_id=world.location,
                quantity=1,
                command_id=uuid4(),
            )
    with as_actor(rbac_graph, world.nurse):
        with pytest.raises(InventoryConflictError, match="batch_expired"):
            use(
                clinic_id=rbac_graph.clinic_a,
                batch_id=batches[0].pk,
                location_id=world.location,
                quantity=1,
                command_id=uuid4(),
            )
        use(
            clinic_id=rbac_graph.clinic_a,
            batch_id=batches[1].pk,
            location_id=world.location,
            quantity=1,
            command_id=uuid4(),
        )
