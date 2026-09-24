from __future__ import annotations

from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlsplit

import pytest
from apps.identity.models import User
from django.test import Client

from auth.totp_decorator_urls import BODY_SENTINEL, QUERY_SENTINEL
from otp_test_support import (
    OTP_RAW_CREDENTIAL,
    create_receptionist,
    create_totp_device,
    fixed_otp_time,
    get_totp_device,
    login,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from django.http import HttpResponseBase

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.urls("totp_decorator_urls"),
]

BLOCK_PATH: Final = "/decorator/clinic-alpha/block-beta/"
KEYWORD_PATH: Final = "/decorator-keyword/clinic-alpha/"
NOISY_BLOCK_PATH: Final = f"{BLOCK_PATH}?probe={QUERY_SENTINEL}"


def _client_for(username: str, *, password: str) -> Client:
    client = Client()
    with runtime_role():
        assert client.login(username=username, password=password)
    return client


def _privileged_client(graph: RbacGraph) -> Client:
    username = User.objects.get(pk=graph.physician).username
    return _client_for(username, password=RBAC_RAW_CREDENTIAL)


def _receptionist_client(graph: RbacGraph) -> Client:
    user = create_receptionist(graph)
    return _client_for(user.username, password=OTP_RAW_CREDENTIAL)


def _verified_privileged_client(graph: RbacGraph) -> Client:
    create_totp_device(graph.physician, confirmed=True)
    username = User.objects.get(pk=graph.physician).username
    client = Client()
    with runtime_role(), fixed_otp_time():
        login(client, username, next_url=BLOCK_PATH, password=RBAC_RAW_CREDENTIAL)
        device = get_totp_device(graph.physician, confirmed=True)
        client.post(
            "/auth/verify/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
                "next": BLOCK_PATH,
            },
        )
    return client


def _challenge_target(response: HttpResponseBase) -> str:
    location = response.headers.get("Location") or response.headers.get("HX-Redirect")
    assert location is not None
    query = parse_qs(urlsplit(location).query)
    targets = query.get("next")
    assert targets is not None
    assert len(targets) == 1
    return targets[0]


def test_verified_privileged_view_receives_route_arguments(
    rbac_graph: RbacGraph,
) -> None:
    client = _verified_privileged_client(rbac_graph)

    with runtime_role():
        response = client.get(BLOCK_PATH)

    assert response.status_code == 200
    assert response.content == b"GET|clinic-alpha|block-beta"


def test_non_privileged_view_receives_route_arguments(
    rbac_graph: RbacGraph,
) -> None:
    client = _receptionist_client(rbac_graph)

    with runtime_role():
        response = client.get(BLOCK_PATH)

    assert response.status_code == 200
    assert response.content == b"GET|clinic-alpha|block-beta"


def test_non_privileged_unsafe_method_receives_route_arguments(
    rbac_graph: RbacGraph,
) -> None:
    client = _receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(BLOCK_PATH, {"probe": BODY_SENTINEL})

    assert response.status_code == 200
    assert response.content == b"POST|clinic-alpha|block-beta"


def test_non_privileged_view_receives_keyword_route_arguments(
    rbac_graph: RbacGraph,
) -> None:
    client = _receptionist_client(rbac_graph)

    with runtime_role():
        response = client.get(KEYWORD_PATH)

    assert response.status_code == 200
    assert response.content == b"GET|clinic-alpha"


def test_unverified_privileged_safe_get_continues_to_full_path(
    rbac_graph: RbacGraph,
) -> None:
    client = _privileged_client(rbac_graph)

    with runtime_role():
        response = client.get(NOISY_BLOCK_PATH)

    assert response.status_code == 302
    assert _challenge_target(response) == NOISY_BLOCK_PATH


def test_unverified_privileged_post_continues_to_declared_safe_get(
    rbac_graph: RbacGraph,
) -> None:
    client = _privileged_client(rbac_graph)

    with runtime_role():
        response = client.post(NOISY_BLOCK_PATH, {"probe": BODY_SENTINEL})

    assert response.status_code == 302
    assert _challenge_target(response) == BLOCK_PATH


def test_unverified_privileged_htmx_post_uses_hx_redirect_continuation(
    rbac_graph: RbacGraph,
) -> None:
    client = _privileged_client(rbac_graph)

    with runtime_role():
        response = client.post(
            NOISY_BLOCK_PATH,
            {"probe": BODY_SENTINEL},
            headers={"hx-request": "true"},
        )

    assert response.status_code == 204
    assert "HX-Redirect" in response.headers
    assert _challenge_target(response) == BLOCK_PATH


def test_unverified_privileged_post_leaks_no_body_or_query_sentinel(
    rbac_graph: RbacGraph,
) -> None:
    client = _privileged_client(rbac_graph)

    with runtime_role():
        response = client.post(NOISY_BLOCK_PATH, {"probe": BODY_SENTINEL})

    location = response.headers["Location"]
    assert BODY_SENTINEL not in location
    assert QUERY_SENTINEL not in location
    serialized_session = repr(dict(client.session.items()))
    assert BODY_SENTINEL not in serialized_session
    assert QUERY_SENTINEL not in serialized_session


def test_unauthenticated_post_is_refused_without_reflecting_input() -> None:
    client = Client()

    response = client.post(NOISY_BLOCK_PATH, {"probe": BODY_SENTINEL})

    assert response.status_code == 403
    assert BODY_SENTINEL.encode() not in response.content
    assert QUERY_SENTINEL.encode() not in response.content


def test_unverified_privileged_keyword_post_continues_to_keyword_get(
    rbac_graph: RbacGraph,
) -> None:
    client = _privileged_client(rbac_graph)

    with runtime_role():
        response = client.post(KEYWORD_PATH, {"probe": BODY_SENTINEL})

    assert response.status_code == 302
    assert _challenge_target(response) == KEYWORD_PATH
