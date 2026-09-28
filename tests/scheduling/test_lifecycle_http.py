"""Agenda lifecycle route (todo 22): native POST arrival, identical silent refusals."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.scheduling.models import Appointment, AppointmentTransition
from apps.tenancy.db import tenant_context

from identity.permission_support import owner_context
from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from scheduling.appointment_http_support import agenda_at_url
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _url(clinic_id: object) -> str:
    return f"/scheduling/clinics/{clinic_id}/appointments/lifecycle/"


def _payload(appointment: Appointment, **changes: str) -> dict[str, str]:
    return {
        "appointment_id": str(appointment.pk),
        "action": "arrive",
        "expected_revision": str(appointment.revision),
        "command_id": str(uuid4()),
        **changes,
    }


def _traces(organization: UUID) -> tuple[int, int, list[tuple[str, int]]]:
    with owner_context(organization):
        return (
            AppointmentTransition.objects.count(),
            AuditEvent.objects.count(),
            list(Appointment.objects.order_by("pk").values_list("status", "revision")),
        )


def _post(client: Client, url: str, data: dict[str, str]) -> _MonkeyPatchedWSGIResponse:
    with runtime_role():
        before = dict(client.session)
        response = client.post(url, data)
        assert dict(client.session) == before
    return response


def test_reception_records_arrival_and_replays_the_command(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client, _ = receptionist_client(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    with runtime_role():
        agenda = client.get(agenda_at_url(setup.clinic_id, "day", "2035-06-02"))
    assert b'name="action" value="arrive"' in agenda.content
    payload = _payload(appointment)
    response = _post(client, _url(setup.clinic_id), payload)
    assert response.status_code == 303
    assert response["Location"] == agenda_at_url(setup.clinic_id, "day", "2035-06-02")
    replay = _post(client, _url(setup.clinic_id), payload)
    assert replay.status_code == 303
    with owner_context(setup.organization_id):
        appointment.refresh_from_db()
        assert appointment.status == "arrived"
        assert (
            AppointmentTransition.objects.filter(
                command_id=payload["command_id"]
            ).count()
            == 1
        )
    stale = _post(
        client, _url(setup.clinic_id), _payload(appointment, expected_revision="1")
    )
    assert stale.status_code == 409
    assert b'id="lifecycle-conflict"' in stale.content


def test_refusals_are_identical_and_leave_no_trace(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client, _ = receptionist_client(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    # The real flow reaches the route from the agenda, which selects the clinic.
    with runtime_role():
        client.get(agenda_at_url(setup.clinic_id, "day", "2035-06-02"))
    before = _traces(setup.organization_id)
    unknown = _post(
        client,
        _url(setup.clinic_id),
        _payload(appointment, appointment_id=str(uuid4())),
    )
    foreign = _post(client, _url(rbac_graph.clinic_b), _payload(appointment))
    refusals = [
        unknown,
        foreign,
        # Reception holds no clinician (own move_own) authority to complete.
        _post(client, _url(setup.clinic_id), _payload(appointment, action="complete")),
        _post(client, _url(setup.clinic_id), _payload(appointment, action="drop")),
        _post(
            client, _url(setup.clinic_id), _payload(appointment, expected_revision="x")
        ),
        _post(client, _url(setup.clinic_id), {**_payload(appointment), "clinic": "x"}),
        _post(client, _url(setup.clinic_id), {}),
    ]
    assert {response.status_code for response in refusals} == {404}
    assert len({response.content for response in refusals}) == 1
    assert all("Set-Cookie" not in response for response in refusals)
    assert all(not response.cookies for response in refusals)
    assert _traces(setup.organization_id) == before
