"""Display possession is an independent read-only principal, never a staff grant."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from apps.scheduling.reception import revoke_display_device
from apps.tenancy.db import tenant_context
from django.db import connection

from identity.legacy_parity_support import world
from identity.reception_subjects import seed_reception
from identity.sql_guard_probes import ALL_ROLES
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_reception_display_device_contract(rbac_graph: RbacGraph, role: str) -> None:
    subject = world(rbac_graph, role)
    subjects = seed_reception(rbac_graph, subject.appointment)
    actor = subject.actor
    digest = hashlib.sha256(subjects.device.token.encode("ascii")).hexdigest()
    with (
        runtime_role(),
        tenant_context(actor.pk, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.reception_display(%s)", [digest])
        connected = cursor.fetchone()
        assert connected is not None
        assert connected[0] is not None
        assert set(json.loads(connected[0])) == {"tickets"}
        for invalid in ("", "0" * 64, "not-a-digest"):
            cursor.execute("SELECT clinic_app.reception_display(%s)", [invalid])
            assert cursor.fetchone() == (None,)
    with (
        runtime_role(),
        tenant_context(
            subjects.setup.booking.actor_id, subjects.setup.booking.organization_id
        ),
    ):
        revoke_display_device(
            clinic_id=subjects.setup.booking.clinic_id,
            device_id=subjects.device.device_id,
        )
    with (
        runtime_role(),
        tenant_context(actor.pk, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.reception_display(%s)", [digest])
        assert cursor.fetchone() == (None,)
