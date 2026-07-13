from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.contrib.staticfiles import finders
from django.test import Client, override_settings

from otp_test_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _logged_in_client(graph: RbacGraph) -> Client:
    client = Client()
    username = User.objects.get(pk=graph.physician).username
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
    return client


def test_login_screen_uses_external_design_system_styles_and_accessible_form() -> None:
    response = Client().get("/auth/login/")

    assert response.status_code == 200
    assert b'href="/static/css/clinic-os.css"' in response.content
    assert b"<style" not in response.content
    assert b'<label for="id_username">' in response.content
    assert b'<label for="id_password">' in response.content
    assert b'aria-describedby="login-help"' in response.content
    assert b"Sign in to Clinic OS" in response.content


def test_clinic_stylesheet_is_discoverable_by_django_staticfiles() -> None:
    assert finders.find("css/clinic-os.css") is not None


def test_auth_responses_are_uncacheable_and_vary_for_htmx() -> None:
    response = Client().get("/auth/login/", HTTP_HX_REQUEST="true")

    assert "no-store" in response.headers["Cache-Control"]
    assert "HX-Request" in response.headers["Vary"]


@override_settings(DEBUG=True)
def test_auth_showcase_is_debug_only_and_contains_inert_required_states(
    rbac_graph: RbacGraph,
) -> None:
    client = _logged_in_client(rbac_graph)

    with runtime_role():
        response = client.get("/__ui__/auth/")

    assert response.status_code == 200
    for state in (b"Default", b"Focus", b"Error", b"Disabled", b"Loading", b"Success"):
        assert state in response.content
    assert b"QR placeholder" in response.content
    assert b"otpauth://" not in response.content
    assert b"data:image/png" not in response.content


@override_settings(DEBUG=False)
def test_auth_showcase_is_unavailable_outside_debug(
    rbac_graph: RbacGraph,
) -> None:
    client = _logged_in_client(rbac_graph)

    with runtime_role():
        response = client.get("/__ui__/auth/")

    assert response.status_code == 404


def test_htmx_login_uses_safe_client_redirect_header(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {
                "username": username,
                "password": RBAC_RAW_CREDENTIAL,
                "next": "/auth/protected/",
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/auth/protected/"


def test_logout_requires_confirmation_post_and_clears_session(
    rbac_graph: RbacGraph,
) -> None:
    client = _logged_in_client(rbac_graph)

    with runtime_role():
        confirmation = client.get("/auth/logout/")
        response = client.post("/auth/logout/")

    assert confirmation.status_code == 200
    assert b"Sign out" in confirmation.content
    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/login/"
    assert "active_org_id" not in client.session


def test_logout_post_requires_csrf_and_preserves_the_session(
    rbac_graph: RbacGraph,
) -> None:
    client = Client(enforce_csrf_checks=True)
    username = User.objects.get(pk=rbac_graph.physician).username
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        response = client.post("/auth/logout/")

    assert response.status_code == 403
    assert "active_org_id" in client.session


def test_invalid_login_returns_plain_recovery_copy_without_authentication(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {"username": username, "password": "wrong-password"},
        )

    assert response.status_code == 200
    assert b"Check your username and password, then try again." in response.content
    assert "active_org_id" not in client.session
