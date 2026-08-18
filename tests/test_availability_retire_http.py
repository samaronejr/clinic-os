from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from django.test import Client

from availability_http_support import (
    MORNING,
    RETIRED_EVENT,
    availability_list_url,
    availability_retire_url,
    block_is_retired,
    book_inside_block,
    create_dst_clinic,
    grant_role,
    seed_block,
)
from otp_test_support import OTP_RAW_CREDENTIAL, login, runtime_role
from patient_http_support import (
    audit_event_types,
    receptionist_client,
    verified_physician_client,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SEE_OTHER: Final = 303


def _seeded(rbac_graph: RbacGraph) -> tuple[Client, UUID, UUID]:
    client, receptionist = receptionist_client(rbac_graph)
    block = seed_block(
        rbac_graph, receptionist.pk, rbac_graph.clinic_a, rbac_graph.physician
    )
    return client, receptionist.pk, block.pk


def test_manager_retires_an_empty_block(rbac_graph: RbacGraph) -> None:
    client, actor, block_id = _seeded(rbac_graph)

    with runtime_role():
        response = client.post(availability_retire_url(rbac_graph.clinic_a, block_id))

    assert response.status_code == SEE_OTHER
    assert response.headers["Location"] == availability_list_url(rbac_graph.clinic_a)
    assert block_is_retired(rbac_graph, actor, block_id)
    assert audit_event_types(rbac_graph, actor).count(RETIRED_EVENT) == 1


def test_retirement_refuses_every_safe_method(rbac_graph: RbacGraph) -> None:
    client, actor, block_id = _seeded(rbac_graph)
    url = availability_retire_url(rbac_graph.clinic_a, block_id)

    with runtime_role():
        statuses = [client.get(url).status_code, client.head(url).status_code]

    assert statuses == [405, 405]
    assert not block_is_retired(rbac_graph, actor, block_id)


def test_route_clinic_must_match_the_stored_block_clinic(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, block_id = _seeded(rbac_graph)
    other_clinic = create_dst_clinic(rbac_graph, actor, rbac_graph.physician)

    with runtime_role():
        response = client.post(availability_retire_url(other_clinic, block_id))

    assert response.status_code == 404
    assert not block_is_retired(rbac_graph, actor, block_id)
    assert RETIRED_EVENT not in audit_event_types(rbac_graph, actor)


def test_physician_cannot_retire_a_block(rbac_graph: RbacGraph) -> None:
    _client, actor, block_id = _seeded(rbac_graph)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        response = physician.post(
            availability_retire_url(rbac_graph.clinic_a, block_id)
        )

    assert response.status_code == 404
    assert not block_is_retired(rbac_graph, actor, block_id)


def test_foreign_and_unknown_block_identifiers_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, block_id = _seeded(rbac_graph)
    grant_role(
        rbac_graph,
        actor,
        rbac_graph.clinic_b,
        UserClinicRole.Role.RECEPTIONIST,
    )

    with runtime_role():
        unknown = client.post(availability_retire_url(rbac_graph.clinic_a, uuid4()))
        foreign_clinic = client.post(
            availability_retire_url(rbac_graph.clinic_b, block_id)
        )

    assert {unknown.status_code, foreign_clinic.status_code} == {404}
    assert not block_is_retired(rbac_graph, actor, block_id)


def test_block_with_a_future_appointment_is_never_retired(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, block_id = _seeded(rbac_graph)
    book_inside_block(
        rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician, MORNING
    )

    with runtime_role():
        response = client.post(availability_retire_url(rbac_graph.clinic_a, block_id))

    assert response.status_code == 200
    assert b'id="scheduling-retire-error"' in response.content
    assert not block_is_retired(rbac_graph, actor, block_id)
    assert RETIRED_EVENT not in audit_event_types(rbac_graph, actor)


def test_retirement_without_csrf_token_is_rejected(rbac_graph: RbacGraph) -> None:
    _client, receptionist = receptionist_client(rbac_graph)
    block = seed_block(
        rbac_graph, receptionist.pk, rbac_graph.clinic_a, rbac_graph.physician
    )
    strict = Client(enforce_csrf_checks=True)

    with runtime_role():
        assert strict.login(username=receptionist.username, password=OTP_RAW_CREDENTIAL)
        response = strict.post(availability_retire_url(rbac_graph.clinic_a, block.pk))

    assert response.status_code == 403
    assert not block_is_retired(rbac_graph, receptionist.pk, block.pk)


def test_repeated_retirement_is_an_idempotent_no_op(rbac_graph: RbacGraph) -> None:
    client, actor, block_id = _seeded(rbac_graph)
    url = availability_retire_url(rbac_graph.clinic_a, block_id)

    with runtime_role():
        first = client.post(url)
        second = client.post(url)

    assert [first.status_code, second.status_code] == [SEE_OTHER, SEE_OTHER]
    assert audit_event_types(rbac_graph, actor).count(RETIRED_EVENT) == 1


def test_htmx_retirement_redirects_without_a_document_body(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, block_id = _seeded(rbac_graph)

    with runtime_role():
        response = client.post(
            availability_retire_url(rbac_graph.clinic_a, block_id),
            headers={"hx-request": "true"},
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == availability_list_url(rbac_graph.clinic_a)
    assert block_is_retired(rbac_graph, actor, block_id)


def test_retirement_responses_are_private_and_uncacheable(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, block_id = _seeded(rbac_graph)

    with runtime_role():
        response = client.post(availability_retire_url(rbac_graph.clinic_a, block_id))

    cache_control = response.headers["Cache-Control"]
    for directive in ("private", "no-store", "no-cache", "must-revalidate"):
        assert directive in cache_control
    assert "HX-Request" in response.headers["Vary"]


@pytest.mark.parametrize("htmx", [False, True])
def test_unverified_privileged_retirement_resumes_at_the_clinic_list_get(
    rbac_graph: RbacGraph,
    *,
    htmx: bool,
) -> None:
    _client, actor, block_id = _seeded(rbac_graph)
    grant_role(
        rbac_graph,
        rbac_graph.physician,
        rbac_graph.clinic_a,
        UserClinicRole.Role.CLINIC_ADMIN,
    )
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username
    list_url = availability_list_url(rbac_graph.clinic_a)

    with runtime_role():
        login(client, username, password=RBAC_RAW_CREDENTIAL)
        response = client.post(
            availability_retire_url(rbac_graph.clinic_a, block_id),
            headers={"hx-request": "true"} if htmx else {},
        )

    location = response.headers["HX-Redirect"] if htmx else response.headers["Location"]
    assert response.status_code == (204 if htmx else 302)
    assert list_url.replace("/", "%2F") in location
    assert str(block_id) not in location
    assert "retire" not in location
    assert not block_is_retired(rbac_graph, actor, block_id)
    assert str(block_id) not in str(dict(client.session))
