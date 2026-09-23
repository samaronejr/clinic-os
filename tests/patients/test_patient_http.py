from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User
from django.test import Client

from otp_test_support import OTP_RAW_CREDENTIAL, login, runtime_role
from patient_http_support import (
    SEARCH_EVENT,
    audit_event_types,
    audit_payloads,
    patient_list_url,
    receptionist_client,
    seed_patients,
    verified_physician_client,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

QUERY_SENTINEL: Final = "Marina"
SEEDED: Final = (
    "Marina Synthetic Alpha",
    "Marina Synthetic Beta",
    "Marina Synthetic Gamma",
    "Rui Synthetic Delta",
)
FORM_ACTION = re.compile(rb'<form[^>]*action="([^"]*)"')
ANCHOR_HREF = re.compile(rb'<a[^>]*href="([^"]*)"')
FORM_METHOD = re.compile(rb"<form[^>]*>")


def _seed(graph: RbacGraph) -> tuple[Client, UUID, str]:
    client, receptionist = receptionist_client(graph)
    seed_patients(graph, receptionist.pk, graph.clinic_a, SEEDED)
    return client, receptionist.pk, patient_list_url(graph.clinic_a)


def _names(content: bytes) -> list[str]:
    return [
        name
        for name in (item.rsplit(" ", 1)[0] for item in SEEDED)
        if name.encode() in content
    ]


def test_get_patient_list_renders_only_a_blank_search(rbac_graph: RbacGraph) -> None:
    client, actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.get(url)

    assert response.status_code == 200
    assert b"Marina Synthetic Alpha" not in response.content
    assert SEARCH_EVENT not in audit_event_types(rbac_graph, actor)


def test_get_with_query_parameters_never_searches(rbac_graph: RbacGraph) -> None:
    client, actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.get(f"{url}?q={QUERY_SENTINEL}&birth_date=1990-01-01&page=1")

    assert response.status_code == 200
    assert b"Marina Synthetic Alpha" not in response.content
    assert SEARCH_EVENT not in audit_event_types(rbac_graph, actor)


def test_post_search_returns_only_the_selected_clinic(rbac_graph: RbacGraph) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    seed_patients(rbac_graph, receptionist.pk, rbac_graph.clinic_a, SEEDED[:3])
    seed_patients(
        rbac_graph,
        rbac_graph.clinic_admin,
        rbac_graph.clinic_b,
        ("Marina Synthetic Foreign",),
    )

    with runtime_role():
        response = client.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": QUERY_SENTINEL, "page": "1"},
        )

    assert response.status_code == 200
    assert b"Marina Synthetic Alpha" in response.content
    assert b"Marina Synthetic Foreign" not in response.content


def test_post_search_orders_deterministically(rbac_graph: RbacGraph) -> None:
    client, _actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.post(url, {"q": QUERY_SENTINEL, "page": "1"})

    content = response.content
    positions = [
        content.index(name.encode())
        for name in ("Marina Synthetic Alpha", "Marina Synthetic Beta")
    ]
    assert positions == sorted(positions)
    assert b"Rui Synthetic Delta" not in content


def test_pagination_is_post_only_and_keeps_state_out_of_urls(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    names = tuple(f"Marina Synthetic P{index:03d}" for index in range(30))
    seed_patients(rbac_graph, receptionist.pk, rbac_graph.clinic_a, names)
    url = patient_list_url(rbac_graph.clinic_a)

    with runtime_role():
        first = client.post(url, {"q": QUERY_SENTINEL, "page": "1"})
        second = client.post(url, {"q": QUERY_SENTINEL, "page": "2"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert b"Marina Synthetic P000" in first.content
    assert b"Marina Synthetic P000" not in second.content
    assert b"Marina Synthetic P025" in second.content
    for action in FORM_ACTION.findall(first.content):
        assert b"?" not in action
    for href in ANCHOR_HREF.findall(first.content):
        assert QUERY_SENTINEL.encode() not in href
        assert b"birth_date" not in href
    assert b'method="get"' not in first.content.lower()


def test_post_search_without_csrf_token_is_rejected(rbac_graph: RbacGraph) -> None:
    _, receptionist = receptionist_client(rbac_graph)
    actor = receptionist.pk
    strict = Client(enforce_csrf_checks=True)
    with runtime_role():
        assert strict.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
        response = strict.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": QUERY_SENTINEL, "page": "1"},
        )

    assert response.status_code == 403
    assert SEARCH_EVENT not in audit_event_types(rbac_graph, actor)


def test_physician_cannot_search(rbac_graph: RbacGraph) -> None:
    client = verified_physician_client(rbac_graph)
    actor = rbac_graph.clinic_admin

    with runtime_role():
        response = client.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": QUERY_SENTINEL, "page": "1"},
        )

    assert response.status_code == 404
    assert SEARCH_EVENT not in audit_event_types(rbac_graph, actor)


def test_physician_denial_is_indistinguishable_from_foreign_clinic(
    rbac_graph: RbacGraph,
) -> None:
    physician = verified_physician_client(rbac_graph)
    receptionist, _ = receptionist_client(rbac_graph)

    with runtime_role():
        denied_role = physician.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": QUERY_SENTINEL, "page": "1"},
        )
        denied_clinic = receptionist.post(
            patient_list_url(rbac_graph.clinic_c),
            {"q": QUERY_SENTINEL, "page": "1"},
        )
        denied_unknown = receptionist.post(
            patient_list_url(uuid4()),
            {"q": QUERY_SENTINEL, "page": "1"},
        )

    statuses = {
        denied_role.status_code,
        denied_clinic.status_code,
        denied_unknown.status_code,
    }
    assert statuses == {404}


def test_accepted_search_emits_exactly_one_metadata_only_event(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.post(url, {"q": QUERY_SENTINEL, "page": "1"})

    assert response.status_code == 200
    assert audit_event_types(rbac_graph, actor).count(SEARCH_EVENT) == 1
    payload = audit_payloads(rbac_graph, actor, SEARCH_EVENT)
    assert sorted(entry["key"] for entry in payload) == ["clinic_id", "object_verb"]
    values = {entry["value"] for entry in payload}
    assert QUERY_SENTINEL not in " ".join(values)


def test_search_places_no_demographics_or_identifiers_in_url_or_access_log(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.post(
            url,
            {"q": QUERY_SENTINEL, "birth_date": "1990-01-01", "page": "1"},
        )

    access_log_line = response.wsgi_request.get_full_path()
    assert access_log_line == url
    assert "?" not in access_log_line
    assert QUERY_SENTINEL not in access_log_line
    assert "1990-01-01" not in access_log_line
    assert response.headers.get("Location") is None


def test_htmx_post_search_returns_a_results_fragment(rbac_graph: RbacGraph) -> None:
    client, _actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.post(
            url,
            {"q": QUERY_SENTINEL, "page": "1"},
            headers={"hx-request": "true"},
        )

    assert response.status_code == 200
    assert b"<html" not in response.content.lower()
    assert b"Marina Synthetic Alpha" in response.content


def test_intake_responses_are_private_and_uncacheable(rbac_graph: RbacGraph) -> None:
    client, _actor, url = _seed(rbac_graph)

    with runtime_role():
        response = client.post(url, {"q": QUERY_SENTINEL, "page": "1"})

    cache_control = response.headers["Cache-Control"]
    for directive in ("private", "no-store", "no-cache", "must-revalidate"):
        assert directive in cache_control
    assert "HX-Request" in response.headers["Vary"]


def test_unverified_privileged_search_post_continues_to_the_clinic_list_get(
    rbac_graph: RbacGraph,
) -> None:
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username
    with runtime_role():
        login(client, username, password=RBAC_RAW_CREDENTIAL)
        response = client.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": QUERY_SENTINEL, "page": "1"},
        )

    assert response.status_code == 302
    location = response.headers["Location"]
    assert QUERY_SENTINEL not in location
    assert patient_list_url(rbac_graph.clinic_a).replace("/", "%2F") in location
