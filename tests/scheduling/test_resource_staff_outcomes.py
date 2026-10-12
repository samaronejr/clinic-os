"""RP staff can finish service booking and moving through their real responses."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.models import Appointment, AppointmentResource
from apps.scheduling.timezones import parse_local_minute
from apps.tenancy.db import tenant_context
from django.urls import reverse

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.test_resource_role_matrix import _verified_client
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]


@pytest.mark.parametrize(
    "role", [UserClinicRole.Role.SCHEDULER, UserClinicRole.Role.CLINIC_MANAGER]
)
@pytest.mark.parametrize("htmx", [False, True])
def test_authorized_staff_prepare_book_move_and_reach_safe_destinations(
    rbac_graph: RbacGraph, role: UserClinicRole.Role, *, htmx: bool
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
    actor = create_role_actor(rbac_graph, role)
    client = _verified_client(actor)
    headers = {"HX-Request": "true"} if htmx else {}
    key = str(uuid4())
    payload = {
        "enrollment_id": str(setup.enrollment_id),
        "practitioner": str(setup.practitioner_id),
        "service_type_id": str(service.pk),
        "resource_ids": [str(room.pk), str(equipment.pk)],
        "start_local": "2035-06-02T08:10",
        "end_local": "2035-06-02T08:40",
        "idempotency_key": key,
    }
    url = reverse("scheduling:appointment-create", args=(setup.clinic_id,))
    with runtime_role():
        prepared = client.post(url, {**payload, "mode": "prepare"}, headers=headers)
        assert prepared.status_code == 200
        assert prepared.context["list_url"] == reverse(
            "scheduling:resource-settings", args=(setup.clinic_id,)
        )
        assert client.get(prepared.context["list_url"]).status_code == 200
        created = client.post(url, {**payload, "mode": "create"}, headers=headers)
        assert created.status_code == (204 if htmx else 303)
        destination = created["HX-Redirect" if htmx else "Location"]
        agenda = client.get(destination)
        assert agenda.status_code == 200
    assert str(setup.enrollment_id) not in destination
    with runtime_role(), tenant_context(actor.pk, setup.organization_id):
        appointment = Appointment.objects.get(idempotency_key=key)
        reservations = list(AppointmentResource.objects.filter(appointment=appointment))
        assert {row.resource_id for row in reservations} == {room.pk, equipment.pk}
        assert all(
            row.start_at == appointment.start_at - timedelta(minutes=10)
            and row.end_at == appointment.end_at + timedelta(minutes=10)
            and row.occupied
            for row in reservations
        )
        identifier = appointment.pk
    with runtime_role():
        transition_url = reverse(
            "scheduling:appointment-reschedule", args=(identifier,)
        )
        assert transition_url.encode() in agenda.content
        assert client.get(transition_url).status_code == 200
        moved = client.post(
            transition_url,
            {"start_local": "2035-06-02T09:10", "end_local": "2035-06-02T09:40"},
            headers=headers,
        )
        assert moved.status_code == (204 if htmx else 303)
        assert (
            client.get(moved["HX-Redirect" if htmx else "Location"]).status_code == 200
        )
    with runtime_role(), tenant_context(actor.pk, setup.organization_id):
        appointment.refresh_from_db()
        assert appointment.start_at == parse_local_minute(
            "2035-06-02T09:10", "America/Sao_Paulo"
        )
        reservations = list(AppointmentResource.objects.filter(appointment=appointment))
        assert len(reservations) == 2
        assert all(
            row.start_at == appointment.start_at - timedelta(minutes=10)
            and row.end_at == appointment.end_at + timedelta(minutes=10)
            and row.occupied
            for row in reservations
        )
