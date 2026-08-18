from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from otp_test_support import runtime_role
from patient_http_support import (
    audit_event_types,
    patient_create_url,
    patient_list_url,
    receptionist_client,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_create_get_generates_a_fresh_idempotency_key(rbac_graph: RbacGraph) -> None:
    client, _ = receptionist_client(rbac_graph)
    url = patient_create_url(rbac_graph.clinic_a)

    with runtime_role():
        first = client.get(url)
        second = client.get(url)

    keys = re.compile(rb'name="idempotency_key"[^>]*value="([0-9a-f-]{36})"')
    first_key = keys.search(first.content)
    second_key = keys.search(second.content)
    assert first_key is not None
    assert second_key is not None
    assert first_key.group(1) != second_key.group(1)


def test_create_post_redirects_without_any_identifier_in_the_location(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)
    url = patient_create_url(rbac_graph.clinic_a)

    with runtime_role():
        response = client.post(
            url,
            {
                "full_name": "Nina Synthetic Testpatient",
                "birth_date": "1988-04-05",
                "idempotency_key": str(uuid4()),
            },
        )

    assert response.status_code == 303
    location = response.headers["Location"]
    assert location == patient_list_url(rbac_graph.clinic_a)
    assert "Nina" not in location
    assert "1988-04-05" not in location


def test_create_post_htmx_returns_204_with_hx_redirect(rbac_graph: RbacGraph) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            {
                "full_name": "Otto Synthetic Testpatient",
                "birth_date": "1979-02-03",
                "idempotency_key": str(uuid4()),
            },
            headers={"hx-request": "true"},
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == patient_list_url(rbac_graph.clinic_a)


def test_create_post_invalid_input_returns_200_without_reflecting_it(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            {
                "full_name": "  ",
                "birth_date": "3999-01-01",
                "idempotency_key": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert response.headers.get("Location") is None


def test_create_post_equal_replay_is_idempotent(rbac_graph: RbacGraph) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    actor = receptionist.pk
    key = str(uuid4())
    payload = {
        "full_name": "Pia Synthetic Testpatient",
        "birth_date": "1991-06-07",
        "idempotency_key": key,
    }

    with runtime_role():
        first = client.post(patient_create_url(rbac_graph.clinic_a), payload)
        second = client.post(patient_create_url(rbac_graph.clinic_a), payload)

    assert first.status_code == 303
    assert second.status_code == 303
    assert audit_event_types(rbac_graph, actor).count("intake.patient.created") == 1
