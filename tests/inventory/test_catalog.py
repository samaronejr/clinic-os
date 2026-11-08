"""Catalog commands, encrypted supplier fields, and closed audit envelopes."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.inventory import services
from apps.inventory.models import Batch, CommandReceipt, Item, Supplier
from django.core.exceptions import ValidationError
from django.db import connection

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from inventory.support import stock_world

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
SENTINEL = "SINTETICO-SENTINELA-61"


def test_catalog_commands_retain_history_and_keep_supplier_content_encrypted(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    command = uuid4()
    with as_actor(rbac_graph, world.manager):
        item = services.create_item(
            clinic_id=rbac_graph.clinic_a,
            name="Synthetic consumable",
            unit="each",
            command_id=command,
        )
        assert (
            services.create_item(
                clinic_id=rbac_graph.clinic_a,
                name="Synthetic consumable",
                unit="each",
                command_id=command,
            ).pk
            == item.pk
        )
        with pytest.raises(services.InventoryConflictError, match="idempotency"):
            services.create_location(
                clinic_id=rbac_graph.clinic_a, name="Synthetic", command_id=command
            )
        location = services.create_location(
            clinic_id=rbac_graph.clinic_a, name="Synthetic store", command_id=uuid4()
        )
        supplier = services.create_supplier(
            clinic_id=rbac_graph.clinic_a,
            name=SENTINEL,
            contact=f"{SENTINEL}@example.invalid",
            command_id=uuid4(),
        )
        assert Supplier.objects.get(pk=supplier.pk).name == SENTINEL
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name,contact FROM clinic_app.inventory_supplier WHERE id=%s",
                [supplier.pk],
            )
            encrypted = cursor.fetchone()
        assert encrypted is not None
        assert all(SENTINEL.encode() not in bytes(value) for value in encrypted)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.inventory_today(%s)", [rbac_graph.clinic_a]
            )
            local_today = cursor.fetchone()[0]
        batch = services.create_batch(
            clinic_id=rbac_graph.clinic_a,
            item_id=item.pk,
            lot="SYNTHETIC-EXPIRING",
            expires_on=local_today + timedelta(days=2),
            command_id=uuid4(),
        )
        services.receive(
            clinic_id=rbac_graph.clinic_a,
            batch_id=batch.pk,
            location_id=location.pk,
            quantity=2,
            command_id=uuid4(),
        )
        for quantity in (4, 1):
            services.set_minimum_stock(
                clinic_id=rbac_graph.clinic_a,
                item_id=item.pk,
                location_id=location.pk,
                quantity=quantity,
                command_id=uuid4(),
            )
        alerts = services.stock_alerts(clinic_id=rbac_graph.clinic_a)
        assert (batch.pk, "expiring") in {(a.batch_id, a.kind) for a in alerts}
        assert (item.pk, location.pk, "below_minimum") not in {
            (a.item_id, a.location_id, a.kind) for a in alerts
        }
    with owner_context(rbac_graph.organization_a):
        assert CommandReceipt.objects.count() == 7
        events = list(
            AuditEvent.objects.filter(event_type__startswith="inventory.").values(
                "event_type", "payload"
            )
        )
        assert len(events) == 7
        assert SENTINEL not in json.dumps(events)
        assert all(
            set(event["payload"])
            <= {
                "clinic_id",
                "http_method",
                "http_status",
                "object_verb",
                "reason_code",
                "request_id",
            }
            for event in events
        )
        assert IntegrationOperation.objects.count() == 0


@pytest.mark.parametrize("name", ["", " ", "<script>", "a\nb", "x" * 161])
def test_catalog_rejects_nonlabels(rbac_graph: RbacGraph, name: str) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.manager), pytest.raises(ValidationError):
        services.create_item(
            clinic_id=rbac_graph.clinic_a, name=name, unit="each", command_id=uuid4()
        )


def test_catalog_validates_contact_date_and_command_at_boundary(
    rbac_graph: RbacGraph,
) -> None:
    world = stock_world(rbac_graph)
    with as_actor(rbac_graph, world.manager):
        with pytest.raises(ValidationError):
            services.create_supplier(
                clinic_id=rbac_graph.clinic_a,
                name="Synthetic",
                contact="x" * 1001,
                command_id=uuid4(),
            )
        with pytest.raises(ValidationError):
            services.create_batch(
                clinic_id=rbac_graph.clinic_a,
                item_id=world.item,
                lot="Synthetic",
                expires_on=cast("date", "2999-01-01"),
                command_id=uuid4(),
            )
        with pytest.raises(ValidationError):
            services.create_location(
                clinic_id=rbac_graph.clinic_a,
                name="Synthetic",
                command_id=cast("UUID", "not-a-uuid"),
            )


@pytest.mark.parametrize("foreign", [False, True])
def test_unknown_and_other_clinic_batches_have_identical_denial(
    rbac_graph: RbacGraph, foreign: bool
) -> None:
    world = stock_world(rbac_graph)
    selector = uuid4()
    if foreign:
        with owner_context(rbac_graph.organization_b):
            item = Item.objects.create(
                organization_id=rbac_graph.organization_b,
                clinic_id=rbac_graph.clinic_c,
                name="Synthetic",
            )
            selector = Batch.objects.create(
                organization_id=rbac_graph.organization_b,
                clinic_id=rbac_graph.clinic_c,
                item=item,
                lot="SYNTHETIC",
                expires_on=date(2999, 1, 1),
            ).pk
    with as_actor(rbac_graph, world.manager):
        with pytest.raises(services.InventoryAccessDeniedError) as refusal:
            services.balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=selector,
                location_id=world.location,
            )
        assert str(refusal.value) == ""
        with pytest.raises(services.InventoryAccessDeniedError) as refusal:
            services.balance(
                clinic_id=rbac_graph.clinic_a,
                batch_id=world.batch,
                location_id=uuid4(),
            )
        assert str(refusal.value) == ""
        with pytest.raises(services.InventoryAccessDeniedError):
            services.return_stock(
                clinic_id=rbac_graph.clinic_a,
                use_movement_id=uuid4(),
                quantity=1,
                command_id=uuid4(),
            )
