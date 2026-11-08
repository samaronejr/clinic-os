"""Approval and budget fences do not silently order or dispense materials."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.identity.models import RoleGrant
from apps.inventory import services
from apps.inventory.models import MinimumStock, PurchaseBudget, PurchaseRequest
from apps.inventory.services import (
    InventoryAccessDeniedError,
    InventoryConflictError,
    approve_purchase,
    balance,
    create_purchase_request,
    equipment_status,
    propose_replenishment,
    receive_purchase,
    record_calibration,
    record_maintenance,
    register_equipment,
    stock_alerts,
)
from django.core.exceptions import ValidationError
from django.utils import timezone

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from inventory.support import stock_world, stocked

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_minimum_stock_alert_is_scoped_to_item_and_location(
    rbac_graph: RbacGraph,
) -> None:
    world = stocked(rbac_graph, quantity=2)
    with owner_context(rbac_graph.organization_a):
        MinimumStock.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            item_id=world.item,
            location_id=world.location,
            quantity=3,
        )
    with as_actor(rbac_graph, world.manager):
        alerts = stock_alerts(clinic_id=rbac_graph.clinic_a)
        assert (world.item, world.location, "below_minimum") in {
            (alert.item_id, alert.location_id, alert.kind) for alert in alerts
        }


def test_purchase_approval_and_receipt_are_separate_idempotent_commands(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.nurse):
        request = create_purchase_request(
            clinic_id=rbac_graph.clinic_a,
            item_id=world.item,
            supplier_id=world.supplier,
            quantity=5,
            amount_minor=500,
            command_id=uuid4(),
        )
        assert request.state == "draft"
        with pytest.raises(InventoryAccessDeniedError):
            approve_purchase(
                clinic_id=rbac_graph.clinic_a,
                purchase_request_id=request.pk,
                command_id=uuid4(),
            )
    with as_actor(rbac_graph, world.manager):
        with pytest.raises(InventoryConflictError, match="purchase_not_approved"):
            receive_purchase(
                clinic_id=rbac_graph.clinic_a,
                purchase_request_id=request.pk,
                batch_id=world.batch,
                location_id=world.location,
                command_id=uuid4(),
            )
        approved = approve_purchase(
            clinic_id=rbac_graph.clinic_a,
            purchase_request_id=request.pk,
            command_id=uuid4(),
        )
        assert approved.state == "approved"
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 0
        )
        command = uuid4()
        received = receive_purchase(
            clinic_id=rbac_graph.clinic_a,
            purchase_request_id=request.pk,
            batch_id=world.batch,
            location_id=world.location,
            command_id=command,
        )
        replay = receive_purchase(
            clinic_id=rbac_graph.clinic_a,
            purchase_request_id=request.pk,
            batch_id=world.batch,
            location_id=world.location,
            command_id=command,
        )
        assert received.pk == replay.pk
        assert received.state == "received"
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 5
        )


def test_replenishment_is_only_a_draft_within_remaining_approved_budget(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        budget = PurchaseBudget.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            amount_minor=1000,
            approved_by_id=world.manager,
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with as_actor(rbac_graph, world.manager):
        proposal = propose_replenishment(
            clinic_id=rbac_graph.clinic_a,
            budget_id=budget.pk,
            item_id=world.item,
            supplier_id=world.supplier,
            quantity=5,
            amount_minor=600,
            command_id=uuid4(),
        )
        assert proposal.state == "draft"
        approve_purchase(
            clinic_id=rbac_graph.clinic_a,
            purchase_request_id=proposal.pk,
            command_id=uuid4(),
        )
        with pytest.raises(InventoryConflictError, match="budget_exceeded"):
            propose_replenishment(
                clinic_id=rbac_graph.clinic_a,
                budget_id=budget.pk,
                item_id=world.item,
                supplier_id=world.supplier,
                quantity=5,
                amount_minor=600,
                command_id=uuid4(),
            )
        assert (
            balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=world.location,
            )
            == 0
        )
    with owner_context(rbac_graph.organization_a):
        assert PurchaseRequest.objects.count() == 1


def test_equipment_retains_calibration_and_maintenance_history(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.manager):
        equipment = register_equipment(
            clinic_id=rbac_graph.clinic_a,
            supplier_id=world.supplier,
            location_id=world.location,
            name="Synthetic equipment",
            serial="SYNTHETIC-61",
            command_id=uuid4(),
        )
        calibration = record_calibration(
            clinic_id=rbac_graph.clinic_a,
            equipment_id=equipment.pk,
            performed_on=date(1999, 1, 1),
            due_on=date(2000, 1, 1),
            outcome="failed",
            command_id=uuid4(),
        )
        maintenance = record_maintenance(
            clinic_id=rbac_graph.clinic_a,
            equipment_id=equipment.pk,
            performed_on=date(2026, 1, 2),
            due_on=date(2999, 1, 1),
            outcome="completed",
            command_id=uuid4(),
        )
        status = equipment_status(
            clinic_id=rbac_graph.clinic_a, equipment_id=equipment.pk
        )
        assert status.calibration_id == calibration.pk
        assert status.maintenance_id == maintenance.pk
        assert status.calibration_overdue is True
        assert status.usable is False


def test_equipment_requires_current_successful_calibration_and_maintenance(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.manager):
        equipment = register_equipment(
            clinic_id=rbac_graph.clinic_a,
            supplier_id=world.supplier,
            location_id=world.location,
            name="Synthetic equipment",
            serial="SYNTHETIC-CURRENT",
            command_id=uuid4(),
        )
        initial = equipment_status(
            clinic_id=rbac_graph.clinic_a, equipment_id=equipment.pk
        )
        assert initial.calibration_id is None
        assert initial.maintenance_id is None
        assert initial.usable is False
        with pytest.raises(ValidationError):
            record_calibration(
                clinic_id=rbac_graph.clinic_a,
                equipment_id=equipment.pk,
                performed_on=cast("date", "invalid"),
                due_on=date(2999, 1, 1),
                outcome="passed",
                command_id=uuid4(),
            )
        with pytest.raises(ValidationError):
            record_calibration(
                clinic_id=rbac_graph.clinic_a,
                equipment_id=equipment.pk,
                performed_on=date(2026, 1, 1),
                due_on=date(2025, 1, 1),
                outcome="passed",
                command_id=uuid4(),
            )
        record_calibration(
            clinic_id=rbac_graph.clinic_a,
            equipment_id=equipment.pk,
            performed_on=date(2026, 1, 1),
            due_on=date(2999, 1, 1),
            outcome="passed",
            command_id=uuid4(),
        )
        assert equipment_status(
            clinic_id=rbac_graph.clinic_a, equipment_id=equipment.pk
        ).usable
        record_maintenance(
            clinic_id=rbac_graph.clinic_a,
            equipment_id=equipment.pk,
            performed_on=date(2026, 1, 1),
            due_on=date(2999, 1, 1),
            outcome="failed",
            command_id=uuid4(),
        )
        assert not equipment_status(
            clinic_id=rbac_graph.clinic_a, equipment_id=equipment.pk
        ).usable


@pytest.mark.parametrize("kind", ["calibration", "maintenance"])
def test_future_equipment_receipts_cannot_establish_current_usability(
    rbac_graph: RbacGraph, kind: str
) -> None:
    world = stock_world(rbac_graph)
    future = timezone.now().date() + timedelta(days=2)
    with as_actor(rbac_graph, world.manager):
        equipment = register_equipment(
            clinic_id=rbac_graph.clinic_a,
            supplier_id=world.supplier,
            location_id=world.location,
            name="Synthetic future evidence probe",
            serial="SYNTHETIC-FUTURE",
            command_id=uuid4(),
        )
        receipt = record_calibration if kind == "calibration" else record_maintenance
        with pytest.raises(ValidationError):
            receipt(
                clinic_id=rbac_graph.clinic_a,
                equipment_id=equipment.pk,
                performed_on=future,
                due_on=future + timedelta(days=30),
                outcome="passed" if kind == "calibration" else "completed",
                command_id=uuid4(),
            )


@pytest.mark.parametrize("budget", [None, "not-a-uuid"])
def test_replenishment_never_creates_an_unbudgeted_proposal(
    rbac_graph: RbacGraph, budget: object
) -> None:
    world = stock_world(rbac_graph)
    with (
        as_actor(rbac_graph, world.nurse),
        pytest.raises(InventoryAccessDeniedError),
    ):
        propose_replenishment(
            clinic_id=rbac_graph.clinic_a,
            budget_id=cast("UUID", budget),
            item_id=world.item,
            supplier_id=world.supplier,
            quantity=1,
            amount_minor=100,
            command_id=uuid4(),
        )
    with owner_context(rbac_graph.organization_a):
        assert PurchaseRequest.objects.count() == 0


def test_subtracting_use_preserves_inventory_management_authority(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.nurse):
        request = create_purchase_request(
            clinic_id=rbac_graph.clinic_a,
            item_id=world.item,
            supplier_id=world.supplier,
            quantity=3,
            amount_minor=300,
            command_id=uuid4(),
        )
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="clinic_manager",
            permission="inventory.use",
            bundle_version=2,
            valid_from=timezone.now(),
        )
    with as_actor(rbac_graph, world.manager):
        item = services.create_item(
            clinic_id=rbac_graph.clinic_a,
            name="Synthetic management-only item",
            unit="each",
            command_id=uuid4(),
        )
        assert item.pk is not None
        services.approve_budget(
            clinic_id=rbac_graph.clinic_a, amount_minor=500, command_id=uuid4()
        )
        approve_purchase(
            clinic_id=rbac_graph.clinic_a,
            purchase_request_id=request.pk,
            command_id=uuid4(),
        )
        receive_purchase(
            clinic_id=rbac_graph.clinic_a,
            purchase_request_id=request.pk,
            batch_id=world.batch,
            location_id=world.location,
            command_id=uuid4(),
        )
        services.write_off(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=1,
            reason="damaged",
            command_id=uuid4(),
        )
        services.transfer(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            source_location_id=world.location,
            destination_location_id=world.other_location,
            quantity=1,
            command_id=uuid4(),
        )
        services.receive(
            clinic_id=rbac_graph.clinic_a,
            batch_id=world.batch,
            location_id=world.location,
            quantity=2,
            command_id=uuid4(),
        )
        with pytest.raises(InventoryAccessDeniedError):
            services.use(
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
            == 3
        )
