from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.scheduling.models import Appointment
from django.test import Client

from appointment_http_support import (
    APPOINTMENT_CANCELLED_EVENT,
    APPOINTMENT_RESCHEDULED_EVENT,
    APPOINTMENT_VIEWED_EVENT,
    BOUNDARY_END,
    BOUNDARY_START,
    INSIDE_END,
    INSIDE_START,
    OUTSIDE_END,
    OUTSIDE_START,
    SYNTHETIC_PATIENT,
    BookingContext,
    appointment_create_url,
    cancel_url,
    create_payload,
    reload_appointment,
    reschedule_payload,
    reschedule_url,
    seed_appointment,
    seed_enrollment,
)
from availability_http_support import grant_role, seed_block
from otp_test_support import OTP_RAW_CREDENTIAL, login, runtime_role
from patient_http_support import (
    audit_event_types,
    receptionist_client,
    verified_physician_client,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SEE_OTHER: Final = 303
PATIENT_REQUEST: Final = "patient_request"


def _scheduled(graph: RbacGraph) -> tuple[Client, BookingContext, UUID, UUID]:
    client, receptionist = receptionist_client(graph)
    context = BookingContext(graph, receptionist.pk, graph.clinic_a)
    seed_block(graph, receptionist.pk, graph.clinic_a, graph.physician)
    enrollment_id = seed_enrollment(graph, receptionist.pk, graph.clinic_a)
    appointment = seed_appointment(context, enrollment_id, graph.physician)
    return client, context, appointment.pk, enrollment_id


def test_reschedule_get_audits_the_read_and_offers_only_a_new_window(
    rbac_graph: RbacGraph,
) -> None:
    client, context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.get(reschedule_url(appointment_id))

    body = response.content
    assert response.status_code == 200
    assert SYNTHETIC_PATIENT.encode() in body
    assert INSIDE_START.encode() in body
    assert b"America/Sao_Paulo" in body
    assert b'name="practitioner"' not in body
    assert b'name="enrollment_id"' not in body
    assert b"1988-04-05" not in body
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(APPOINTMENT_VIEWED_EVENT) == 1


def test_reschedule_post_moves_the_window_and_returns_to_the_same_object(
    rbac_graph: RbacGraph,
) -> None:
    client, context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.post(
            reschedule_url(appointment_id),
            reschedule_payload(BOUNDARY_START, BOUNDARY_END),
        )

    moved = reload_appointment(rbac_graph, context.actor, appointment_id)
    assert response.status_code == SEE_OTHER
    assert response.headers["Location"] == reschedule_url(appointment_id)
    assert moved.status == Appointment.Status.SCHEDULED
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(APPOINTMENT_RESCHEDULED_EVENT) == 1


def test_htmx_reschedule_redirects_to_the_same_object_without_a_body(
    rbac_graph: RbacGraph,
) -> None:
    client, _context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.post(
            reschedule_url(appointment_id),
            reschedule_payload(BOUNDARY_START, BOUNDARY_END),
            headers={"hx-request": "true"},
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == reschedule_url(appointment_id)


def test_reschedule_outside_the_promised_window_is_refused(
    rbac_graph: RbacGraph,
) -> None:
    client, context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.post(
            reschedule_url(appointment_id),
            reschedule_payload(OUTSIDE_START, OUTSIDE_END),
        )

    unchanged = reload_appointment(rbac_graph, context.actor, appointment_id)
    assert response.status_code == 200
    assert b'id="transition-errors"' in response.content
    assert unchanged.status == Appointment.Status.SCHEDULED
    events = audit_event_types(rbac_graph, context.actor)
    assert APPOINTMENT_RESCHEDULED_EVENT not in events


def test_cancel_offers_only_the_closed_reason_vocabulary(
    rbac_graph: RbacGraph,
) -> None:
    client, _context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.get(cancel_url(appointment_id))

    body = response.content
    assert response.status_code == 200
    assert body.count(b"<option") == len(Appointment.CancellationReason.choices)
    assert b'type="text"' not in body
    assert b"<textarea" not in body


def test_cancel_post_is_terminal_and_returns_to_the_same_object(
    rbac_graph: RbacGraph,
) -> None:
    client, context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.post(cancel_url(appointment_id), {"reason": PATIENT_REQUEST})
        repeated = client.post(cancel_url(appointment_id), {"reason": PATIENT_REQUEST})
        blocked = client.post(
            reschedule_url(appointment_id),
            reschedule_payload(BOUNDARY_START, BOUNDARY_END),
        )

    cancelled = reload_appointment(rbac_graph, context.actor, appointment_id)
    assert [response.status_code, repeated.status_code] == [SEE_OTHER, SEE_OTHER]
    assert response.headers["Location"] == cancel_url(appointment_id)
    assert cancelled.status == Appointment.Status.CANCELLED
    assert blocked.status_code == 200
    assert b"cancelled and can no longer be moved" in blocked.content
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(APPOINTMENT_CANCELLED_EVENT) == 1


def test_a_reason_outside_the_vocabulary_never_cancels(
    rbac_graph: RbacGraph,
) -> None:
    client, context, appointment_id, _enrollment = _scheduled(rbac_graph)

    with runtime_role():
        response = client.post(cancel_url(appointment_id), {"reason": "because"})

    unchanged = reload_appointment(rbac_graph, context.actor, appointment_id)
    assert response.status_code == 200
    assert b'id="transition-errors"' in response.content
    assert unchanged.status == Appointment.Status.SCHEDULED


def test_a_cancelled_window_can_be_booked_again(rbac_graph: RbacGraph) -> None:
    client, context, appointment_id, enrollment_id = _scheduled(rbac_graph)

    with runtime_role():
        client.post(cancel_url(appointment_id), {"reason": PATIENT_REQUEST})
        response = client.post(
            appointment_create_url(context.clinic_id),
            create_payload(
                enrollment_id, rbac_graph.physician, INSIDE_START, INSIDE_END
            ),
        )

    assert response.status_code == SEE_OTHER
    events = audit_event_types(rbac_graph, context.actor)
    assert events.count(APPOINTMENT_CANCELLED_EVENT) == 1


def test_a_physician_and_a_foreign_identifier_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    client, _context, appointment_id, _enrollment = _scheduled(rbac_graph)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        denied = physician.get(reschedule_url(appointment_id))
        cancelled = physician.post(
            cancel_url(appointment_id), {"reason": PATIENT_REQUEST}
        )
        unknown = client.get(cancel_url(uuid4()))

    assert {denied.status_code, cancelled.status_code, unknown.status_code} == {404}


def test_transitions_without_a_csrf_token_are_rejected(
    rbac_graph: RbacGraph,
) -> None:
    _client, receptionist = receptionist_client(rbac_graph)
    context = BookingContext(rbac_graph, receptionist.pk, rbac_graph.clinic_a)
    seed_block(rbac_graph, receptionist.pk, rbac_graph.clinic_a, rbac_graph.physician)
    enrollment_id = seed_enrollment(rbac_graph, receptionist.pk, rbac_graph.clinic_a)
    appointment = seed_appointment(context, enrollment_id, rbac_graph.physician)
    strict = Client(enforce_csrf_checks=True)

    with runtime_role():
        assert strict.login(username=receptionist.username, password=OTP_RAW_CREDENTIAL)
        response = strict.post(cancel_url(appointment.pk), {"reason": PATIENT_REQUEST})

    current = reload_appointment(rbac_graph, receptionist.pk, appointment.pk)
    assert response.status_code == 403
    assert current.status == Appointment.Status.SCHEDULED


@pytest.mark.parametrize("route", ["reschedule", "cancel"])
@pytest.mark.parametrize("htmx", [False, True])
def test_unverified_privileged_transition_resumes_at_its_own_object_get(
    rbac_graph: RbacGraph,
    route: str,
    *,
    htmx: bool,
) -> None:
    _client, context, appointment_id, _enrollment = _scheduled(rbac_graph)
    grant_role(
        rbac_graph,
        rbac_graph.physician,
        rbac_graph.clinic_a,
        UserClinicRole.Role.CLINIC_ADMIN,
    )
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username
    target = (
        reschedule_url(appointment_id)
        if route == "reschedule"
        else cancel_url(appointment_id)
    )
    payload = (
        reschedule_payload(BOUNDARY_START, BOUNDARY_END)
        if route == "reschedule"
        else {"reason": PATIENT_REQUEST}
    )

    with runtime_role():
        login(client, username, password=RBAC_RAW_CREDENTIAL)
        response = client.post(
            target, payload, headers={"hx-request": "true"} if htmx else {}
        )

    location = response.headers["HX-Redirect"] if htmx else response.headers["Location"]
    current = reload_appointment(rbac_graph, context.actor, appointment_id)
    assert response.status_code == (204 if htmx else 302)
    assert target.replace("/", "%2F") in location
    assert BOUNDARY_START not in location
    assert PATIENT_REQUEST not in location
    assert current.status == Appointment.Status.SCHEDULED
    assert BOUNDARY_START not in str(dict(client.session))
