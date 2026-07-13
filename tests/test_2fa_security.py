from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.contrib.auth import SESSION_KEY
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_otp import DEVICE_ID_SESSION_KEY

from otp_test_support import (
    create_totp_device,
    current_user_guc,
    fixed_otp_time,
    get_totp_device,
    login,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _physician_login(client: Client, username: str) -> None:
    assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)


def _enroll(client: Client, graph: RbacGraph) -> TOTPDevice:
    response = client.get("/auth/enroll/")
    assert response.status_code == 200
    started = client.post("/auth/enroll/", {"action": "start"})
    assert started.status_code == 200
    return get_totp_device(graph.physician, confirmed=False)


def test_replayed_totp_is_rejected_after_session_verification_is_cleared(
    rbac_graph: RbacGraph,
) -> None:
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username
    with runtime_role():
        _physician_login(client, username)
        device = _enroll(client, rbac_graph)
        token = token_for(device)
        with fixed_otp_time():
            first = client.post(
                "/auth/enroll/",
                {
                    "action": "confirm",
                    "otp_device": device.persistent_id,
                    "otp_token": token,
                },
            )
        session = client.session
        session.pop(DEVICE_ID_SESSION_KEY)
        session.save()
        with fixed_otp_time():
            replay = client.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token},
            )
        protected = client.get("/auth/protected/")

    assert first.status_code == 302
    assert replay.status_code == 200
    assert b"not valid" in replay.content.lower()
    assert protected.status_code == 302
    assert protected.headers["Location"].startswith("/auth/verify/")


def test_verification_rejects_another_users_device_even_with_valid_token(
    rbac_graph: RbacGraph,
) -> None:
    foreign = create_totp_device(rbac_graph.clinic_admin, confirmed=True)
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username

    with runtime_role():
        _physician_login(client, username)
        own = _enroll(client, rbac_graph)
        with current_user_guc(rbac_graph.physician):
            own.confirmed = True
            own.save(update_fields=("confirmed",))
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {
                    "otp_device": foreign.persistent_id,
                    "otp_token": token_for(foreign),
                },
            )

    assert response.status_code == 200
    assert b"valid choice" in response.content.lower()
    assert DEVICE_ID_SESSION_KEY not in client.session


@pytest.mark.parametrize(
    "next_url",
    [
        "https://attacker.invalid/steal",
        "//attacker.invalid/steal",
        "javascript:alert(1)",
        "\\attacker.invalid\\steal",
    ],
)
def test_login_rejects_hostile_next_urls(
    rbac_graph: RbacGraph,
    next_url: str,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {
                "username": username,
                "password": RBAC_RAW_CREDENTIAL,
                "next": next_url,
            },
        )

    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/protected/"


def test_login_enrollment_and_verify_never_query_identity_user_directly(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role(), CaptureQueriesContext(connection) as queries:
        logged_in = login(client, username, password=RBAC_RAW_CREDENTIAL)
        enrollment = client.get("/auth/enroll/")
        started = client.post("/auth/enroll/", {"action": "start"})
        device = get_totp_device(rbac_graph.physician, confirmed=False)
        with fixed_otp_time():
            verified = client.post(
                "/auth/enroll/",
                {
                    "action": "confirm",
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                },
            )

    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert logged_in.status_code == 302
    assert enrollment.status_code == 200
    assert started.status_code == 200
    assert verified.status_code == 302
    assert "clinic_app.auth_lookup" in statements
    assert "clinic_app.load_current_user()" in statements
    assert 'FROM "identity_user"' not in statements
    assert 'UPDATE "identity_user"' not in statements


@pytest.mark.parametrize("htmx", [False, True])
def test_login_requires_csrf_for_standard_and_htmx_requests(
    rbac_graph: RbacGraph,
    htmx: bool,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client(enforce_csrf_checks=True)

    with runtime_role():
        if htmx:
            response = client.post(
                "/auth/login/",
                {"username": username, "password": RBAC_RAW_CREDENTIAL},
                headers={"HX-Request": "true"},
            )
        else:
            response = client.post(
                "/auth/login/",
                {"username": username, "password": RBAC_RAW_CREDENTIAL},
            )

    assert response.status_code == 403
    assert SESSION_KEY not in client.session


def _tenant_gucs() -> tuple[str | None, str | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.current_user_id', true), "
            "current_setting('app.current_tenant', true)"
        )
        row = cursor.fetchone()
    assert row is not None
    user_guc, tenant_guc = row
    assert user_guc is None or isinstance(user_guc, str)
    assert tenant_guc is None or isinstance(tenant_guc, str)
    return user_guc, tenant_guc


def test_auth_flow_clears_poisoned_and_request_scoped_tenant_gucs(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, false)",
                [str(rbac_graph.clinic_admin)],
            )
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
                [str(rbac_graph.organization_b)],
            )
        logged_in = login(client, username, password=RBAC_RAW_CREDENTIAL)
        assert _tenant_gucs() == ("", "")
        enrollment = client.get("/auth/enroll/")
        assert _tenant_gucs() == ("", "")
        started = client.post("/auth/enroll/", {"action": "start"})
        assert _tenant_gucs() == ("", "")
        device = get_totp_device(rbac_graph.physician, confirmed=False)
        with fixed_otp_time():
            confirmed = client.post(
                "/auth/enroll/",
                {
                    "action": "confirm",
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                },
            )
        assert _tenant_gucs() == ("", "")
        logged_out = client.post("/auth/logout/")
        assert _tenant_gucs() == ("", "")

    assert logged_in.status_code == 302
    assert enrollment.status_code == 200
    assert started.status_code == 200
    assert confirmed.status_code == 302
    assert logged_out.status_code == 302
