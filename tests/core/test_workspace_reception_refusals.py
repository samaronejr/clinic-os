"""Parallel-worker witnesses for every new reception HTTP refusal exit."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.reception import mark_arrived
from apps.scheduling.resource_services import ResourceInput, create_resource
from apps.tenancy.db import tenant_context

from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_reception_refusal_exits_are_silent_and_parallel_safe(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client, _ = receptionist_client(rbac_graph)
    url = f"/scheduling/clinics/{setup.clinic_id}/reception/"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        location = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="Synthetic reception", kind="location"),
        )
        room = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="Synthetic room", kind="room"),
        )
    arrival = {
        "intent": "arrive",
        "appointment_id": str(appointment.pk),
        "location_id": str(location.pk),
        "command_id": str(uuid4()),
        "expected_revision": "1",
        "day": "2035-06-02",
    }
    cases: tuple[tuple[str, str, dict[str, str]], ...] = (
        ("get", f"/scheduling/clinics/{uuid4()}/reception/", {}),
        ("post", url, {"intent": "delete"}),
        ("post", url, {"intent": "view", "day": "invalid"}),
        ("post", url, {"intent": "call"}),
        ("post", url, {**arrival, "appointment_id": str(uuid4())}),
        ("post", url, {**arrival, "expected_revision": "bad"}),
        ("post", url, {**arrival, "unexpected": "synthetic"}),
    )
    with runtime_role():
        for method, target, data in cases:
            before = dict(client.session)
            response = (
                client.get(target) if method == "get" else client.post(target, data)
            )
            assert response.status_code == 404
            assert dict(client.session) == before
            assert not response.cookies
        conflict = client.post(url, {**arrival, "expected_revision": "2"})
        assert conflict.status_code == 409
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        entry = mark_arrived(
            clinic_id=setup.clinic_id,
            appointment_id=appointment.pk,
            location_id=location.pk,
            expected_revision=1,
            command_id=uuid4(),
        )
    with runtime_role():
        stale = client.post(
            url,
            {
                "intent": "call",
                "queue_entry_id": str(entry.pk),
                "room_id": str(room.pk),
                "unit": "1",
                "expected_revision": "0",
            },
        )
        assert stale.status_code == 409
        public = client.get("/waiting-display/", HTTP_AUTHORIZATION="Bearer forged")
        assert public.status_code == 403
