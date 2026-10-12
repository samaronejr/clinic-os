from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.scheduling.appointment_persistence import _constraint_name
from apps.scheduling.models import Resource
from apps.scheduling.resource_services import ResourceInput, create_resource
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_resource_capacity_and_generation_constraints_are_exact() -> None:
    expected = {
        "scheduling_resource_capacity_excl": "x",
        "scheduling_availability_resource_excl": "x",
        "scheduling_z_buffer_practitioner_excl": "x",
        "scheduling_template_date_uniq": "u",
        "scheduling_resource_scope": "u",
        "scheduling_service_scope": "u",
        "scheduling_template_scope": "u",
        "scheduling_reservation_appointment_binding": "f",
        "scheduling_reservation_resource_binding": "f",
        "scheduling_service_binding": "f",
        "scheduling_template_binding": "f",
        "scheduling_resource_binding": "f",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname, contype, condeferrable, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace='clinic_app'::regnamespace "
            "AND conname=ANY(%s)",
            [list(expected)],
        )
        rows = {row[0]: row[1:] for row in cursor.fetchall()}
    assert {key: row[0] for key, row in rows.items()} == expected
    assert all(not row[1] for row in rows.values())
    assert (
        rows["scheduling_template_date_uniq"][2]
        == "UNIQUE (template_id, generated_date)"
    )
    reservation = rows["scheduling_resource_capacity_excl"][2]
    assert "resource_id WITH =, unit WITH =" in reservation
    assert "tstzrange(start_at, end_at, '[)'::text) WITH &&" in reservation
    assert "WHERE (occupied)" in reservation


def test_populated_downgrade_refuses_atomically_without_losing_history(
    rbac_graph: RbacGraph,
) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        resource = create_resource(
            clinic_id=rbac_graph.clinic_a,
            content=ResourceInput(name="Sintetico retained resource", kind="room"),
        )
    executor = MigrationExecutor(connection)
    with pytest.raises(IntegrityError) as error:
        executor.migrate([("scheduling", "0004_waitlistentry_waitlistoffer_and_more")])
    assert _constraint_name(error.value) == "scheduling_populated_rollback"
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert Resource.objects.get(pk=resource.pk).active
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM django_migrations WHERE app='scheduling' "
            "AND name='0005_resources_templates'"
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.patient_booking_slots(date,uuid)'::regprocedure)"
        )
        assert "scheduling_holiday" in cursor.fetchone()[0]
