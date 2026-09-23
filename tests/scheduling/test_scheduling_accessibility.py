from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from django.contrib.staticfiles import finders

from accessible_document import Document
from otp_test_support import runtime_role
from patient_http_support import (
    patient_list_url,
    receptionist_client,
    verified_physician_client,
)
from scheduling.appointment_http_support import (
    BOUNDARY_END,
    BOUNDARY_START,
    OUTSIDE_END,
    OUTSIDE_START,
    BookingContext,
    agenda_at_url,
    agenda_url,
    appointment_create_url,
    cancel_url,
    create_payload,
    prepare_payload,
    reschedule_payload,
    reschedule_url,
    seed_appointment,
    seed_enrollment,
)
from scheduling.availability_http_support import FUTURE_DATE, seed_block

if TYPE_CHECKING:
    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

PATIENT_REQUEST = "patient_request"


def _booking(graph: RbacGraph) -> tuple[Client, BookingContext, UUID]:
    client, receptionist = receptionist_client(graph)
    context = BookingContext(graph, receptionist.pk, graph.clinic_a)
    seed_block(graph, receptionist.pk, graph.clinic_a, graph.physician)
    enrollment_id = seed_enrollment(graph, receptionist.pk, graph.clinic_a)
    return client, context, enrollment_id


def _assert_shared_contract(document: Document) -> None:
    assert len(document.tagged("h1")) == 1
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()


def test_booking_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(context.clinic_id),
            prepare_payload(enrollment_id),
        )

    document = Document(response.content)
    assert response.status_code == 200
    _assert_shared_contract(document)
    document.assert_every_form_is_post_with_csrf()
    assert document.attributes_for("id_start_local")["aria-describedby"] == (
        "booking-start-help"
    )
    assert document.attributes_for("booking-windows-status")["role"] == "status"
    assert b'<a class="skip-link" href="#main-content">' in response.content


def test_booking_screen_issues_one_fresh_booking_key(rbac_graph: RbacGraph) -> None:
    client, context, enrollment_id = _booking(rbac_graph)
    url = appointment_create_url(context.clinic_id)

    with runtime_role():
        first = Document(client.post(url, prepare_payload(enrollment_id)).content)
        second = Document(client.post(url, prepare_payload(enrollment_id)).content)

    keys = [
        document.attributes_for("id_idempotency_key")["value"]
        for document in (first, second)
    ]
    assert all(key and UUID(key) for key in keys)
    assert keys[0] != keys[1]


def test_refused_booking_errors_are_described_and_announced(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(context.clinic_id),
            create_payload(enrollment_id, rbac_graph.physician, "", ""),
        )

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_start_local")["aria-describedby"] == (
        "booking-start-help id_start_local_error"
    )
    assert document.attributes_for("booking-errors")["role"] == "alert"
    _assert_shared_contract(document)


def test_populated_agenda_exposes_an_accessible_table(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)
    seed_appointment(context, enrollment_id, rbac_graph.physician)

    with runtime_role():
        response = client.get(agenda_at_url(context.clinic_id, "day", FUTURE_DATE))

    document = Document(response.content)
    assert response.status_code == 200
    _assert_shared_contract(document)
    assert b"<caption>Appointments in this day</caption>" in response.content
    assert document.attributes_for("agenda-status")["role"] == "status"
    assert {attributes.get("scope") for attributes in document.tagged("th")} == {
        "col",
        "row",
    }
    assert document.tagged("form") == []


def test_malformed_agenda_announces_an_accessible_alert(
    rbac_graph: RbacGraph,
) -> None:
    client, context, _enrollment = _booking(rbac_graph)

    with runtime_role():
        response = client.get(agenda_at_url(context.clinic_id, "month", FUTURE_DATE))

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("agenda-error")["role"] == "alert"
    _assert_shared_contract(document)


def test_physician_agenda_is_readable_and_offers_no_write_control(
    rbac_graph: RbacGraph,
) -> None:
    _client, context, enrollment_id = _booking(rbac_graph)
    seed_appointment(context, enrollment_id, rbac_graph.physician)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        response = physician.get(agenda_url(context.clinic_id))

    document = Document(response.content)
    assert response.status_code == 200
    _assert_shared_contract(document)
    assert document.tagged("form") == []
    assert document.tagged("button") == []


def test_reschedule_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)
    appointment = seed_appointment(context, enrollment_id, rbac_graph.physician)

    with runtime_role():
        response = client.get(reschedule_url(appointment.pk))

    document = Document(response.content)
    assert response.status_code == 200
    _assert_shared_contract(document)
    document.assert_every_form_is_post_with_csrf()
    assert document.attributes_for("id_end_local")["aria-describedby"] == (
        "reschedule-end-help"
    )


def test_refused_reschedule_errors_are_described_and_announced(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)
    appointment = seed_appointment(context, enrollment_id, rbac_graph.physician)

    with runtime_role():
        response = client.post(
            reschedule_url(appointment.pk),
            reschedule_payload(OUTSIDE_START, OUTSIDE_END),
            headers={"hx-request": "true"},
        )

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("transition-errors")["role"] == "alert"
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    assert b"<html" not in response.content.lower()


def test_cancel_screen_and_its_terminal_state_stay_accessible(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)
    appointment = seed_appointment(context, enrollment_id, rbac_graph.physician)

    with runtime_role():
        before = client.get(cancel_url(appointment.pk))
        client.post(cancel_url(appointment.pk), {"reason": PATIENT_REQUEST})
        after = client.get(reschedule_url(appointment.pk))

    opened = Document(before.content)
    terminal = Document(after.content)
    _assert_shared_contract(opened)
    opened.assert_every_form_is_post_with_csrf()
    assert opened.attributes_for("id_reason")["aria-describedby"] == (
        "cancel-reason-help"
    )
    _assert_shared_contract(terminal)
    assert terminal.tagged("form") == []
    assert terminal.attributes_for("transition-status")["role"] == "status"


def test_patient_results_book_control_stays_accessible(
    rbac_graph: RbacGraph,
) -> None:
    client, context, _enrollment = _booking(rbac_graph)

    with runtime_role():
        response = client.post(patient_list_url(context.clinic_id), {"q": "Nina"})

    document = Document(response.content)
    assert response.status_code == 200
    _assert_shared_contract(document)
    document.assert_every_form_is_post_with_csrf()
    assert {attributes.get("scope") for attributes in document.tagged("th")} == {
        "col",
        "row",
    }
    assert b"Book Nina Synthetic Testpatient" in response.content


def test_booked_agenda_keeps_its_transition_links_state_free(
    rbac_graph: RbacGraph,
) -> None:
    client, context, enrollment_id = _booking(rbac_graph)

    with runtime_role():
        client.post(
            appointment_create_url(context.clinic_id),
            create_payload(
                enrollment_id, rbac_graph.physician, BOUNDARY_START, BOUNDARY_END
            ),
        )
        response = client.get(agenda_at_url(context.clinic_id, "day", FUTURE_DATE))

    document = Document(response.content)
    links = [attributes.get("href") or "" for attributes in document.tagged("a")]
    assert response.status_code == 200
    assert any(link.endswith("/reschedule/") for link in links)
    assert any(link.endswith("/cancel/") for link in links)
    assert all("?" not in link for link in links)
    assert all(str(enrollment_id) not in link for link in links)


def test_scheduling_stylesheet_is_self_hosted_and_discoverable() -> None:
    assert finders.find("css/clinic-os-scheduling.css") is not None
