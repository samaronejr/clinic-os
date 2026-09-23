from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from django.test import Client
from django.urls import reverse

from otp_test_support import OTP_RAW_CREDENTIAL, login, runtime_role
from patient_http_support import (
    audit_event_types,
    patient_list_url,
    receptionist_client,
    verified_physician_client,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from scheduling.appointment_http_support import (
    APPOINTMENT_CREATED_EVENT,
    BOOKING_VIEWED_EVENT,
    BOUNDARY_END,
    BOUNDARY_START,
    CROSS_MIDNIGHT_END,
    CROSS_MIDNIGHT_START,
    INSIDE_END,
    INSIDE_START,
    OUTSIDE_END,
    OUTSIDE_START,
    BookingContext,
    agenda_at_url,
    appointment_create_url,
    appointment_rows,
    booked_local_range,
    create_payload,
    prepare_payload,
    seed_enrollment,
)
from scheduling.availability_http_support import (
    FUTURE_DATE,
    availability_list_url,
    create_dst_clinic,
    grant_role,
    seed_block,
)

if TYPE_CHECKING:
    from apps.scheduling.models import Appointment

    from rbac_fixtures import RbacGraph

CLINIC: UUID = UUID("11111111-1111-4111-8111-111111111111")
APPOINTMENT: UUID = UUID("22222222-2222-4222-8222-222222222222")
SEE_OTHER: Final = 303


def test_appointment_routes_carry_no_patient_state_in_their_paths() -> None:
    create_url = reverse("scheduling:appointment-create", args=(CLINIC,))
    reschedule_url = reverse("scheduling:appointment-reschedule", args=(APPOINTMENT,))
    cancel_url = reverse("scheduling:appointment-cancel", args=(APPOINTMENT,))

    assert create_url == f"/scheduling/clinics/{CLINIC}/appointments/new/"
    assert reschedule_url == f"/scheduling/appointments/{APPOINTMENT}/reschedule/"
    assert cancel_url == f"/scheduling/appointments/{APPOINTMENT}/cancel/"


pytestmark = pytest.mark.django_db(transaction=True)


def _booked_clinic(graph: RbacGraph) -> tuple[Client, UUID, UUID]:
    client, receptionist = receptionist_client(graph)
    seed_block(graph, receptionist.pk, graph.clinic_a, graph.physician)
    enrollment_id = seed_enrollment(graph, receptionist.pk, graph.clinic_a)
    return client, receptionist.pk, enrollment_id


def _appointments(graph: RbacGraph, actor: UUID) -> list[Appointment]:
    return appointment_rows(graph, actor)


def test_prepare_shows_promised_windows_and_explicit_minute_fields(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            prepare_payload(enrollment_id),
        )

    body = response.content
    assert response.status_code == 200
    assert f"{FUTURE_DATE}T08:00".encode() in body
    assert f"{FUTURE_DATE}T09:00".encode() in body
    assert b'name="start_local"' in body
    assert b'name="end_local"' in body
    assert audit_event_types(rbac_graph, actor).count(BOOKING_VIEWED_EVENT) == 1


def test_prepare_generates_no_slot_grid_and_no_fixed_duration(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            prepare_payload(enrollment_id),
        )

    body = response.content
    assert response.status_code == 200
    assert body.count(f"{FUTURE_DATE}T08:".encode()) == 1
    assert b"duration" not in body.lower()
    assert b'type="datetime-local"' in body
    assert b"<option" in body.split(b'name="start_local"')[0].split(b"<form")[-1]


@pytest.mark.parametrize(
    ("start_local", "end_local"),
    [(INSIDE_START, INSIDE_END), (BOUNDARY_START, BOUNDARY_END)],
)
def test_windows_inside_and_on_the_boundary_are_booked(
    rbac_graph: RbacGraph,
    start_local: str,
    end_local: str,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            create_payload(enrollment_id, rbac_graph.physician, start_local, end_local),
        )

    booked = _appointments(rbac_graph, actor)
    context = BookingContext(rbac_graph, actor, rbac_graph.clinic_a)
    assert response.status_code == SEE_OTHER
    assert response.headers["Location"] == agenda_at_url(
        rbac_graph.clinic_a, "day", FUTURE_DATE
    )
    assert str(enrollment_id) not in response.headers["Location"]
    assert len(booked) == 1
    assert booked_local_range(context, booked[0]) == (start_local, end_local)
    assert audit_event_types(rbac_graph, actor).count(APPOINTMENT_CREATED_EVENT) == 1


@pytest.mark.parametrize(
    ("start_local", "end_local"),
    [
        (OUTSIDE_START, OUTSIDE_END),
        (CROSS_MIDNIGHT_START, CROSS_MIDNIGHT_END),
    ],
)
def test_windows_outside_the_block_and_across_midnight_are_refused(
    rbac_graph: RbacGraph,
    start_local: str,
    end_local: str,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            create_payload(enrollment_id, rbac_graph.physician, start_local, end_local),
        )

    assert response.status_code == 200
    assert b'id="booking-errors"' in response.content
    assert _appointments(rbac_graph, actor) == []
    assert APPOINTMENT_CREATED_EVENT not in audit_event_types(rbac_graph, actor)


def test_physician_can_never_prepare_or_create_a_booking(
    rbac_graph: RbacGraph,
) -> None:
    _client, actor, enrollment_id = _booked_clinic(rbac_graph)
    physician = verified_physician_client(rbac_graph)
    url = appointment_create_url(rbac_graph.clinic_a)

    with runtime_role():
        prepared = physician.post(url, prepare_payload(enrollment_id))
        created = physician.post(
            url, create_payload(enrollment_id, rbac_graph.physician)
        )

    assert {prepared.status_code, created.status_code} == {404}
    assert _appointments(rbac_graph, actor) == []


def test_foreign_unknown_and_malformed_selections_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)
    foreign = create_dst_clinic(rbac_graph, actor, rbac_graph.physician)
    url = appointment_create_url(rbac_graph.clinic_a)

    with runtime_role():
        unknown = client.post(url, prepare_payload(uuid4()))
        elsewhere = client.post(
            appointment_create_url(foreign), prepare_payload(enrollment_id)
        )
        malformed = client.post(url, {"mode": "prepare", "enrollment_id": "not-a-uuid"})
        no_mode = client.post(url, {"enrollment_id": str(enrollment_id)})

    statuses = {
        unknown.status_code,
        elsewhere.status_code,
        malformed.status_code,
        no_mode.status_code,
    }
    assert statuses == {404}


def test_booking_without_a_csrf_token_is_rejected(rbac_graph: RbacGraph) -> None:
    _client, receptionist = receptionist_client(rbac_graph)
    seed_block(rbac_graph, receptionist.pk, rbac_graph.clinic_a, rbac_graph.physician)
    enrollment_id = seed_enrollment(rbac_graph, receptionist.pk, rbac_graph.clinic_a)
    strict = Client(enforce_csrf_checks=True)

    with runtime_role():
        assert strict.login(username=receptionist.username, password=OTP_RAW_CREDENTIAL)
        response = strict.post(
            appointment_create_url(rbac_graph.clinic_a),
            create_payload(enrollment_id, rbac_graph.physician),
        )

    assert response.status_code == 403
    assert _appointments(rbac_graph, receptionist.pk) == []


@pytest.mark.parametrize("mode", ["prepare", "create"])
@pytest.mark.parametrize("htmx", [False, True])
def test_unverified_privileged_booking_resumes_at_the_blank_patient_list(
    rbac_graph: RbacGraph,
    mode: str,
    *,
    htmx: bool,
) -> None:
    _client, actor, enrollment_id = _booked_clinic(rbac_graph)
    grant_role(
        rbac_graph,
        rbac_graph.physician,
        rbac_graph.clinic_a,
        UserClinicRole.Role.CLINIC_ADMIN,
    )
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username
    payload = (
        prepare_payload(enrollment_id)
        if mode == "prepare"
        else create_payload(enrollment_id, rbac_graph.physician)
    )

    with runtime_role():
        login(client, username, password=RBAC_RAW_CREDENTIAL)
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            payload,
            headers={"hx-request": "true"} if htmx else {},
        )

    location = response.headers["HX-Redirect"] if htmx else response.headers["Location"]
    assert response.status_code == (204 if htmx else 302)
    assert patient_list_url(rbac_graph.clinic_a).replace("/", "%2F") in location
    assert str(enrollment_id) not in location
    assert "appointments" not in location
    assert INSIDE_START not in location
    assert _appointments(rbac_graph, actor) == []
    assert str(enrollment_id) not in str(dict(client.session))


def test_patient_results_offer_book_as_a_body_only_post(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(patient_list_url(rbac_graph.clinic_a), {"q": "Nina"})

    body = response.content
    assert response.status_code == 200
    assert f'action="{appointment_create_url(rbac_graph.clinic_a)}"'.encode() in body
    assert b'name="mode" value="prepare"' in body
    assert f'name="enrollment_id" value="{enrollment_id}"'.encode() in body
    assert (
        str(enrollment_id).encode()
        not in availability_list_url(rbac_graph.clinic_a).encode()
    )
    assert audit_event_types(rbac_graph, actor).count(BOOKING_VIEWED_EVENT) == 0
