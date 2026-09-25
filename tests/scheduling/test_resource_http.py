from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.intake.patient_access import patient_session_context
from apps.scheduling.models import (
    Appointment,
    AppointmentResource,
    AvailabilityTemplate,
    Resource,
)
from apps.scheduling.patient_booking import (
    patient_slots,
    reschedule_patient_appointment,
)
from apps.scheduling.resource_services import ClosureInput, create_closure
from apps.tenancy.db import tenant_context
from django.utils.translation import gettext

from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from renewal.test_self_booking import _client, _session
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_settings_publish_generate_retire_and_reject_scope_tampering(
    rbac_graph: RbacGraph,
) -> None:
    client, actor = receptionist_client(rbac_graph)
    url = f"/scheduling/clinics/{rbac_graph.clinic_a}/settings/"
    with runtime_role():
        empty = client.get(url)
        assert empty.status_code == 200
        assert "no-store" in empty["Cache-Control"]
        resource = client.post(
            url,
            {
                "action": "resource",
                "resource-name": "Sintetico sala HTTP",
                "resource-kind": "room",
                "resource-capacity": "1",
            },
        )
        assert resource.status_code == 302
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        room = Resource.objects.get(clinic_id=rbac_graph.clinic_a)
    with runtime_role():
        template = client.post(
            url,
            {
                "action": "template",
                "template-resource_id": str(room.pk),
                "template-weekdays": ["5"],
                "template-start_local": "08:00",
                "template-end_local": "12:00",
                "template-valid_from": "2035-06-02",
                "template-valid_to": "2035-06-02",
            },
        )
        assert template.status_code == 302
        service = client.post(
            url,
            {
                "action": "service",
                "service-name": "Sintetico HTTP service",
                "service-duration_min": "30",
                "service-buffer_before": "0",
                "service-buffer_after": "10",
                "service-required_professional_roles": ["physician"],
                "service-required_resource_kinds": ["room"],
                "service-insurer_billable": "on",
                "service-price_ref": "SYNTHETIC-PRICE-1",
            },
        )
        assert service.status_code == 302
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        template_id = AvailabilityTemplate.objects.get(resource=room).pk
    with runtime_role():
        generated = client.post(
            url,
            {
                "action": "generate",
                "generate-template_id": str(template_id),
                "generate-start_date": "2035-06-02",
                "generate-end_date": "2035-06-02",
            },
        )
        assert generated.status_code == 302
        retired = client.post(
            url,
            {
                "action": "retire",
                "retire-kind": "resource",
                "retire-record_id": str(room.pk),
            },
        )
        assert retired.status_code == 302
        assert client.get(url).status_code == 200
        for extra in (
            {"organization_id": str(rbac_graph.organization_b)},
            {"resource-capacity": "0"},
            {"resource-name": "<script>SINTETICO-SENTINELA-RESOURCE</script>"},
        ):
            response = client.post(
                url,
                {
                    "action": "resource",
                    "resource-name": "Sintetico malformed",
                    "resource-kind": "room",
                    "resource-capacity": "1",
                    **extra,
                },
            )
            assert response.status_code == 400
        assert client.post(url, {"action": "unknown"}).status_code == 400
        assert (
            client.post(
                url,
                {
                    "action": "retire",
                    "retire-kind": "resource",
                    "retire-record_id": str(uuid4()),
                },
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/scheduling/clinics/{rbac_graph.clinic_c}/settings/"
            ).status_code
            == 403
        )
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        assert Resource.objects.count() == 1
        assert not Resource.objects.get(pk=room.pk).active


def test_staff_native_booking_uses_selected_service_and_both_resources(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    client, _ = receptionist_client(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
    payload = {
        "mode": "create",
        "enrollment_id": str(setup.enrollment_id),
        "practitioner": str(setup.practitioner_id),
        "start_local": "2035-06-02T09:00",
        "end_local": "2035-06-02T09:30",
        "idempotency_key": str(uuid4()),
        "service_type_id": str(service.pk),
        "resource_ids": [str(room.pk), str(equipment.pk)],
    }
    with runtime_role():
        response = client.post(
            f"/scheduling/clinics/{setup.clinic_id}/appointments/new/", payload
        )
    assert response.status_code == 303
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = Appointment.objects.get(clinic_id=setup.clinic_id)
        assert appointment.service_type_id == service.pk
        assert set(
            AppointmentResource.objects.filter(appointment=appointment).values_list(
                "resource_id", flat=True
            )
        ) == {room.pk, equipment.pk}


def test_patient_keeps_own_service_booking_move_and_cancel_authority(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = book(setup, _catalog(setup))
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        slot = patient_slots(date(2035, 6, 2), appointment.pk)[-2]
        moved = reschedule_patient_appointment(appointment.pk, slot.token)
        assert moved.pk == appointment.pk
        assert moved.start_at == slot.start_at
    with runtime_role():
        response = _client(session_id).post(
            "/patient/appointments/",
            {"action": "cancel", "appointment_id": str(appointment.pk)},
        )
    assert response.status_code == 303
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert not AppointmentResource.objects.filter(
            appointment=appointment, occupied=True
        ).exists()
        assert AppointmentResource.objects.filter(appointment=appointment).count() == 2


def test_patient_stale_slot_after_holiday_returns_portuguese_conflict(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session_id = _session(setup)
    client = _client(session_id)
    with runtime_role(), patient_session_context(session_id):
        stale = patient_slots(date(2035, 6, 2))[0]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        create_closure(
            clinic_id=setup.clinic_id,
            content=ClosureInput(
                start_local="2035-06-02T08:00",
                end_local="2035-06-02T12:00",
                reason="holiday",
            ),
        )
    with runtime_role(), patient_session_context(session_id):
        assert patient_slots(date(2035, 6, 2)) == ()
    with runtime_role():
        response = client.post(
            "/patient/appointments/",
            {
                "action": "book",
                "slot": stale.token,
                "idempotency_key": str(uuid4()),
                "day": "2035-06-02",
            },
        )
    assert response.status_code == 409
    assert (
        gettext("This window overlaps a holiday or absence.").encode()
        in response.content
    )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Appointment.objects.count() == 0
