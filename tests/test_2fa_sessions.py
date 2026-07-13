from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from apps.identity.auth_backends import ClinicBackend
from apps.identity.models import User
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.hashers import make_password
from django.test import Client
from django_otp import DEVICE_ID_SESSION_KEY

from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    get_totp_device,
    runtime_role,
    token_for,
    totp_device_exists,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from conftest import TenantGraph
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _verified_physician_client(graph: RbacGraph) -> Client:
    device = create_totp_device(graph.physician, confirmed=True)
    username = User.objects.get(pk=graph.physician).username
    client = Client()
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                },
            )
        assert response.status_code == 302
        assert client.get("/auth/protected/").status_code == 200
    return client


def _assert_authentication_state_cleared(client: Client) -> None:
    for key in (
        SESSION_KEY,
        BACKEND_SESSION_KEY,
        HASH_SESSION_KEY,
        DEVICE_ID_SESSION_KEY,
        "active_org_id",
    ):
        assert key not in client.session


def test_existing_confirmed_factor_cannot_be_replaced_by_password_only_session(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        response = client.get("/auth/enroll/")
        attempted_start = client.post("/auth/enroll/", {"action": "start"})

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/verify/")
    assert attempted_start.status_code == 302
    assert get_totp_device(rbac_graph.physician, confirmed=True).pk == device.pk
    assert not totp_device_exists(rbac_graph.physician, confirmed=False)


def test_successful_verification_rotates_session_and_invalidates_old_cookie(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        old_session_key = client.session.session_key
        old_cookie = client.cookies["sessionid"].value
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                },
            )
        new_session_key = client.session.session_key
        stolen = Client()
        stolen.cookies["sessionid"] = old_cookie
        stolen_response = stolen.get("/auth/protected/")

    assert response.status_code == 302
    assert old_session_key is not None
    assert new_session_key is not None
    assert new_session_key != old_session_key
    assert stolen_response.status_code == 403


def test_password_relogin_clears_stale_otp_device_session(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {"username": username, "password": RBAC_RAW_CREDENTIAL},
        )
        protected = client.get("/auth/protected/")

    assert response.status_code == 302
    assert DEVICE_ID_SESSION_KEY not in client.session
    assert protected.status_code == 302
    assert protected.headers["Location"].startswith("/auth/verify/")


def test_owner_role_is_not_exempt_from_totp_enforcement(
    tenant_graph: TenantGraph,
) -> None:
    raw_credential = "todo9-owner-password"
    User.objects.filter(pk=tenant_graph.user_a).update(
        password=make_password(raw_credential)
    )
    client = Client()

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {"username": tenant_graph.username_a, "password": raw_credential},
        )
        protected = client.get("/auth/protected/")

    assert response.status_code == 302
    assert protected.status_code == 302
    assert protected.headers["Location"].startswith("/auth/enroll/")


def test_forged_unconfirmed_session_device_never_verifies_user(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        client.post("/auth/enroll/", {"action": "start"})
        device = get_totp_device(rbac_graph.physician, confirmed=False)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
        response = client.get("/auth/protected/")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/enroll/")


def test_deactivated_verified_user_loses_protected_access_immediately(
    rbac_graph: RbacGraph,
) -> None:
    client = _verified_physician_client(rbac_graph)
    User.objects.filter(pk=rbac_graph.physician).update(is_active=False)

    with runtime_role():
        response = client.get("/auth/protected/")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login/")
    _assert_authentication_state_cleared(client)


@pytest.mark.parametrize("path", ["/auth/enroll/", "/auth/verify/", "/auth/protected/"])
def test_password_changed_verified_sessions_fail_safely_on_every_otp_route(
    rbac_graph: RbacGraph,
    path: str,
) -> None:
    client = _verified_physician_client(rbac_graph)
    User.objects.filter(pk=rbac_graph.physician).update(
        password=make_password("todo9-replacement-password")
    )

    with runtime_role():
        response = client.get(path)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login/")
    _assert_authentication_state_cleared(client)


@pytest.mark.parametrize("path", ["/auth/enroll/", "/auth/verify/", "/auth/protected/"])
def test_stale_backend_sessions_fail_safely_on_every_otp_route(
    rbac_graph: RbacGraph,
    path: str,
) -> None:
    client = _verified_physician_client(rbac_graph)

    with (
        runtime_role(),
        patch.object(ClinicBackend, "get_user", return_value=None),
    ):
        response = client.get(path)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login/")
    _assert_authentication_state_cleared(client)
