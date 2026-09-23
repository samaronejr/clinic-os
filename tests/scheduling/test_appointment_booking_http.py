from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest

from otp_test_support import runtime_role
from patient_http_support import audit_event_types, receptionist_client
from scheduling.appointment_http_support import (
    APPOINTMENT_CREATED_EVENT,
    BOUNDARY_END,
    BOUNDARY_START,
    INSIDE_END,
    INSIDE_START,
    agenda_at_url,
    appointment_create_url,
    appointment_rows,
    create_payload,
    prepare_payload,
    seed_enrollment,
)
from scheduling.availability_http_support import FUTURE_DATE, seed_block

if TYPE_CHECKING:
    from uuid import UUID

    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SEE_OTHER: Final = 303


def _booked_clinic(graph: RbacGraph) -> tuple[Client, UUID, UUID]:
    client, receptionist = receptionist_client(graph)
    seed_block(graph, receptionist.pk, graph.clinic_a, graph.physician)
    enrollment_id = seed_enrollment(graph, receptionist.pk, graph.clinic_a)
    return client, receptionist.pk, enrollment_id


def test_prepare_never_accepts_a_safe_method_or_a_query_string(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, enrollment_id = _booked_clinic(rbac_graph)
    url = appointment_create_url(rbac_graph.clinic_a)

    with runtime_role():
        injected = client.get(f"{url}?enrollment_id={enrollment_id}&mode=prepare")
        head = client.head(url)

    assert [injected.status_code, head.status_code] == [405, 405]


def test_equal_replay_of_one_booking_key_creates_exactly_one_appointment(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)
    key = uuid4()
    payload = create_payload(
        enrollment_id, rbac_graph.physician, INSIDE_START, INSIDE_END, key
    )

    with runtime_role():
        first = client.post(appointment_create_url(rbac_graph.clinic_a), payload)
        second = client.post(appointment_create_url(rbac_graph.clinic_a), payload)

    assert [first.status_code, second.status_code] == [SEE_OTHER, SEE_OTHER]
    assert len(appointment_rows(rbac_graph, actor)) == 1
    assert audit_event_types(rbac_graph, actor).count(APPOINTMENT_CREATED_EVENT) == 1


def test_reusing_a_booking_key_for_different_input_conflicts(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)
    key = uuid4()

    with runtime_role():
        client.post(
            appointment_create_url(rbac_graph.clinic_a),
            create_payload(
                enrollment_id, rbac_graph.physician, INSIDE_START, INSIDE_END, key
            ),
        )
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            create_payload(
                enrollment_id, rbac_graph.physician, BOUNDARY_START, BOUNDARY_END, key
            ),
        )

    assert response.status_code == 200
    assert b"already submitted with different details" in response.content
    assert len(appointment_rows(rbac_graph, actor)) == 1


def test_htmx_booking_redirects_without_a_document_body(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            create_payload(enrollment_id, rbac_graph.physician),
            headers={"hx-request": "true"},
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == agenda_at_url(
        rbac_graph.clinic_a, "day", FUTURE_DATE
    )
    assert len(appointment_rows(rbac_graph, actor)) == 1


def test_booking_responses_are_private_and_uncacheable(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, enrollment_id = _booked_clinic(rbac_graph)

    with runtime_role():
        response = client.post(
            appointment_create_url(rbac_graph.clinic_a),
            prepare_payload(enrollment_id),
        )

    cache_control = response.headers["Cache-Control"]
    for directive in ("private", "no-store", "no-cache", "must-revalidate"):
        assert directive in cache_control
    assert "HX-Request" in response.headers["Vary"]
