from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import RoleGrant
from apps.scheduling.models import Appointment, AppointmentResource
from apps.tenancy.db import tenant_context
from django.utils import timezone
from django.utils.translation import gettext

from identity.permission_support import owner_context
from patient_http_support import verified_physician_client
from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_cross_clinic_appointment_setups
from scheduling.resource_test_support import independent_setup
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]


@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize(
    "selection", ["own", "other_clinician", "other_clinic", "unknown"]
)
@pytest.mark.parametrize("mode", ["prepare", "create"])
def test_physician_service_http_authority_matrix(
    rbac_graph: RbacGraph, authorized: bool, selection: str, mode: str
) -> None:
    own, foreign = seed_cross_clinic_appointment_setups(rbac_graph)
    other = independent_setup(own, 1)
    with runtime_role(), tenant_context(own.actor_id, own.organization_id):
        room, equipment, service = _catalog(own)
        _, _, foreign_service = _catalog(foreign)
    client = verified_physician_client(rbac_graph)
    if not authorized:
        with owner_context(own.organization_id):
            RoleGrant.objects.create(
                organization_id=own.organization_id,
                clinic_id=own.clinic_id,
                role="physician",
                permission="appointment.book_own",
                valid_from=timezone.now() - timedelta(days=1),
            )
    service_id = {
        "own": service.pk,
        "other_clinician": service.pk,
        "other_clinic": foreign_service.pk,
        "unknown": uuid4(),
    }[selection]
    practitioner = (
        other.practitioner_id if selection == "other_clinician" else own.practitioner_id
    )
    payload = {
        "mode": mode,
        "enrollment_id": str(own.enrollment_id),
        "practitioner": str(practitioner),
        "service_type_id": str(service_id),
        "resource_ids": [str(room.pk), str(equipment.pk)],
        "start_local": "2035-06-02T09:00",
        "end_local": "2035-06-02T09:30",
        "idempotency_key": str(uuid4()),
    }
    url = f"/scheduling/clinics/{own.clinic_id}/appointments/new/"
    with runtime_role():
        response = client.post(url, payload)
    allowed = authorized and selection == "own"
    assert response.status_code == (
        (200 if mode == "prepare" else 303) if allowed else 404
    )
    if allowed and mode == "prepare":
        assert response.context["form"].initial["service_type_id"] == str(service.pk)
        assert set(response.context["form"].initial["resource_ids"]) == {
            str(room.pk),
            str(equipment.pk),
        }
        assert {row.practitioner_id for row in response.context["practitioners"]} == {
            own.practitioner_id
        }
    if selection in {"other_clinic", "unknown"}:
        counterpart = uuid4() if selection == "other_clinic" else foreign_service.pk
        with runtime_role():
            denied = client.post(url, {**payload, "service_type_id": str(counterpart)})
        assert (response.status_code, response.content) == (
            denied.status_code,
            denied.content,
        )
    with runtime_role(), tenant_context(own.actor_id, own.organization_id):
        if allowed and mode == "create":
            appointment = Appointment.objects.get()
            assert appointment.practitioner_id == own.practitioner_id
            assert appointment.service_type_id == service.pk
            assert set(
                AppointmentResource.objects.values_list("resource_id", flat=True)
            ) == {room.pk, equipment.pk}
        else:
            assert not Appointment.objects.exists()
            assert not AppointmentResource.objects.exists()


def test_own_service_validation_preserves_selection_and_allows_native_retry(
    rbac_graph: RbacGraph,
) -> None:
    own, _ = seed_cross_clinic_appointment_setups(rbac_graph)
    with runtime_role(), tenant_context(own.actor_id, own.organization_id):
        room, equipment, service = _catalog(own)
    client = verified_physician_client(rbac_graph)
    payload = {
        "mode": "create",
        "enrollment_id": str(own.enrollment_id),
        "practitioner": str(own.practitioner_id),
        "service_type_id": str(service.pk),
        "resource_ids": [str(room.pk), str(equipment.pk)],
        "start_local": "2035-06-02T09:00",
        "end_local": "",
        "idempotency_key": str(uuid4()),
    }
    url = f"/scheduling/clinics/{own.clinic_id}/appointments/new/"
    with runtime_role():
        invalid = client.post(url, payload)
        assert invalid.status_code == 200
        assert "end_local" in invalid.context["form"].errors
        assert invalid.context["form"]["service_type_id"].value() == str(service.pk)
        created = client.post(url, {**payload, "end_local": "2035-06-02T09:30"})
    assert created.status_code == 303
    with runtime_role(), tenant_context(own.actor_id, own.organization_id):
        assert Appointment.objects.count() == 1


@pytest.mark.parametrize("mode", ["prepare", "create"])
def test_own_service_missing_required_resource_is_a_conflict(
    rbac_graph: RbacGraph, mode: str
) -> None:
    own, _ = seed_cross_clinic_appointment_setups(rbac_graph)
    with runtime_role(), tenant_context(own.actor_id, own.organization_id):
        _, _, service = _catalog(own)
    client = verified_physician_client(rbac_graph)
    with runtime_role():
        response = client.post(
            f"/scheduling/clinics/{own.clinic_id}/appointments/new/",
            {
                "mode": mode,
                "enrollment_id": str(own.enrollment_id),
                "practitioner": str(own.practitioner_id),
                "service_type_id": str(service.pk),
                "start_local": "2035-06-02T09:00",
                "end_local": "2035-06-02T09:30",
                "idempotency_key": str(uuid4()),
            },
        )
    assert response.status_code == 409
    assert (
        response.content
        == gettext("A required resource is unavailable for this window.").encode()
    )
    with runtime_role(), tenant_context(own.actor_id, own.organization_id):
        assert not Appointment.objects.exists()
