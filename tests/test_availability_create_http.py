from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User
from django.test import Client

from availability_http_support import (
    ADJACENT,
    CREATED_EVENT,
    MORNING,
    LocalWindow,
    active_block_ids,
    availability_list_url,
    create_dst_clinic,
    create_form_payload,
    seed_block,
)
from otp_test_support import OTP_RAW_CREDENTIAL, login, runtime_role
from patient_http_support import (
    audit_event_types,
    audit_payloads,
    receptionist_client,
    verified_physician_client,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

FORM_ACTION: Final = re.compile(rb'<form[^>]*action="([^"]*)"')
PAST_DATE: Final = "2020-05-06"


def _manager(graph: RbacGraph) -> tuple[Client, UUID, str]:
    client, receptionist = receptionist_client(graph)
    return client, receptionist.pk, availability_list_url(graph.clinic_a)


def test_manager_creates_adjacent_local_time_windows(rbac_graph: RbacGraph) -> None:
    client, actor, url = _manager(rbac_graph)

    with runtime_role():
        first = client.post(url, create_form_payload(rbac_graph.physician, MORNING))
        second = client.post(url, create_form_payload(rbac_graph.physician, ADJACENT))

    assert [first.status_code, second.status_code] == [303, 303]
    assert first.headers["Location"] == url
    assert len(active_block_ids(rbac_graph, actor, rbac_graph.clinic_a)) == 2


def test_equal_create_replay_never_duplicates_a_window(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, url = _manager(rbac_graph)
    payload = create_form_payload(rbac_graph.physician, MORNING, uuid4())

    with runtime_role():
        first = client.post(url, payload)
        replay = client.post(url, payload)

    assert [first.status_code, replay.status_code] == [303, 303]
    assert len(active_block_ids(rbac_graph, actor, rbac_graph.clinic_a)) == 1
    assert audit_event_types(rbac_graph, actor).count(CREATED_EVENT) == 1


def test_htmx_create_redirects_without_a_document_body(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, url = _manager(rbac_graph)

    with runtime_role():
        response = client.post(
            url,
            create_form_payload(rbac_graph.physician, MORNING),
            headers={"hx-request": "true"},
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == url
    assert response.content == b""
    assert len(active_block_ids(rbac_graph, actor, rbac_graph.clinic_a)) == 1


def test_physician_cannot_create_availability(rbac_graph: RbacGraph) -> None:
    _client, actor, _url = _manager(rbac_graph)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        response = physician.post(
            availability_list_url(rbac_graph.clinic_a),
            create_form_payload(rbac_graph.physician, MORNING),
        )

    assert response.status_code == 404
    assert active_block_ids(rbac_graph, actor, rbac_graph.clinic_a) == []
    assert CREATED_EVENT not in audit_event_types(rbac_graph, actor)


@pytest.mark.parametrize(
    "window",
    [
        LocalWindow("08:00", "09:00", PAST_DATE),
        LocalWindow("23:00", "01:00"),
        LocalWindow("10:00", "10:00"),
    ],
)
def test_rejected_windows_render_an_error_without_partial_state(
    rbac_graph: RbacGraph,
    window: LocalWindow,
) -> None:
    client, actor, url = _manager(rbac_graph)

    with runtime_role():
        response = client.post(url, create_form_payload(rbac_graph.physician, window))

    assert response.status_code == 200
    assert b'id="scheduling-errors"' in response.content
    assert active_block_ids(rbac_graph, actor, rbac_graph.clinic_a) == []
    assert CREATED_EVENT not in audit_event_types(rbac_graph, actor)


def test_ambiguous_local_minute_is_refused_without_partial_state(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, _url = _manager(rbac_graph)
    clinic_id = create_dst_clinic(rbac_graph, actor, rbac_graph.physician)

    with runtime_role():
        response = client.post(
            availability_list_url(clinic_id),
            create_form_payload(
                rbac_graph.physician,
                LocalWindow("01:30", "01:45", "2031-11-02"),
            ),
        )

    assert response.status_code == 200
    assert b'id="scheduling-errors"' in response.content
    assert active_block_ids(rbac_graph, actor, clinic_id) == []


def test_overlapping_window_is_refused_and_leaves_one_block(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, url = _manager(rbac_graph)
    seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)

    with runtime_role():
        response = client.post(
            url,
            create_form_payload(rbac_graph.physician, LocalWindow("08:30", "09:30")),
        )

    assert response.status_code == 200
    assert b'id="scheduling-errors"' in response.content
    assert len(active_block_ids(rbac_graph, actor, rbac_graph.clinic_a)) == 1


@pytest.mark.parametrize("target", ["clinic_admin", "shared_user"])
def test_non_physician_and_foreign_targets_are_refused(
    rbac_graph: RbacGraph,
    target: str,
) -> None:
    client, actor, url = _manager(rbac_graph)
    practitioner: UUID = getattr(rbac_graph, target)

    with runtime_role():
        response = client.post(url, create_form_payload(practitioner, MORNING))

    assert response.status_code == 200
    assert b'id="id_practitioner_error"' in response.content
    assert active_block_ids(rbac_graph, actor, rbac_graph.clinic_a) == []


def test_create_without_csrf_token_is_rejected(rbac_graph: RbacGraph) -> None:
    _client, receptionist = receptionist_client(rbac_graph)
    strict = Client(enforce_csrf_checks=True)

    with runtime_role():
        assert strict.login(username=receptionist.username, password=OTP_RAW_CREDENTIAL)
        response = strict.post(
            availability_list_url(rbac_graph.clinic_a),
            create_form_payload(rbac_graph.physician, MORNING),
        )

    assert response.status_code == 403
    assert active_block_ids(rbac_graph, receptionist.pk, rbac_graph.clinic_a) == []


def test_foreign_and_unknown_clinics_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, _url = _manager(rbac_graph)

    with runtime_role():
        foreign = client.get(availability_list_url(rbac_graph.clinic_c))
        unknown = client.get(availability_list_url(uuid4()))
        unassigned = client.get(availability_list_url(rbac_graph.clinic_b))

    assert {foreign.status_code, unknown.status_code, unassigned.status_code} == {404}


def test_create_keeps_every_identifier_out_of_urls_and_headers(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, url = _manager(rbac_graph)
    payload = create_form_payload(rbac_graph.physician, MORNING)

    with runtime_role():
        response = client.post(url, payload)
        rendered = client.get(url)

    assert response.wsgi_request.get_full_path() == url
    assert response.headers["Location"] == url
    for value in payload.values():
        assert value not in response.headers["Location"]
    for action in FORM_ACTION.findall(rendered.content):
        assert b"?" not in action


def test_availability_responses_are_private_and_uncacheable(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, url = _manager(rbac_graph)

    with runtime_role():
        response = client.get(url)

    cache_control = response.headers["Cache-Control"]
    for directive in ("private", "no-store", "no-cache", "must-revalidate"):
        assert directive in cache_control
    assert "HX-Request" in response.headers["Vary"]


def test_accepted_create_emits_exactly_one_metadata_only_event(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, url = _manager(rbac_graph)

    with runtime_role():
        response = client.post(url, create_form_payload(rbac_graph.physician, MORNING))

    assert response.status_code == 303
    assert audit_event_types(rbac_graph, actor).count(CREATED_EVENT) == 1
    payload = audit_payloads(rbac_graph, actor, CREATED_EVENT)
    assert sorted(entry["key"] for entry in payload) == ["clinic_id", "object_verb"]
    assert str(rbac_graph.physician) not in " ".join(
        entry["value"] for entry in payload
    )


@pytest.mark.parametrize("htmx", [False, True])
def test_unverified_privileged_create_continues_to_the_clinic_list_get(
    rbac_graph: RbacGraph,
    *,
    htmx: bool,
) -> None:
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username
    url = availability_list_url(rbac_graph.clinic_a)
    payload = create_form_payload(rbac_graph.physician, MORNING)

    with runtime_role():
        login(client, username, password=RBAC_RAW_CREDENTIAL)
        response = client.post(
            url,
            payload,
            headers={"hx-request": "true"} if htmx else {},
        )

    location = response.headers["HX-Redirect"] if htmx else response.headers["Location"]
    assert response.status_code == (204 if htmx else 302)
    assert url.replace("/", "%2F") in location
    assert payload["idempotency_key"] not in location
    assert payload["start_time"].replace(":", "%3A") not in location
    assert active_block_ids(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a) == []
    assert payload["idempotency_key"] not in str(dict(client.session))
