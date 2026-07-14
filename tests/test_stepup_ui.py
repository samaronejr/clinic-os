from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.test import Client

from otp_test_support import create_totp_device, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class _StepUpParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id is not None:
            self.attributes_by_id[element_id] = attributes


def _step_up_client(graph: RbacGraph) -> Client:
    client = Client()
    username = User.objects.get(pk=graph.physician).username
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
    return client


def test_step_up_page_uses_existing_accessible_auth_primitives(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = _step_up_client(rbac_graph)

    with runtime_role():
        response = client.get("/auth/step-up/")

    parser = _StepUpParser()
    parser.feed(response.content.decode())
    token = parser.attributes_by_id["id_otp_token"]
    form = parser.attributes_by_id["step-up-form"]
    assert response.status_code == 200
    assert b'<h1 id="auth-title">Confirm this sensitive action</h1>' in response.content
    assert b'<label for="id_otp_token">Authentication code:</label>' in response.content
    assert token["autocomplete"] == "one-time-code"
    assert token["aria-describedby"] == "otp-help"
    assert form["data-auth-form"] is None
    assert device.persistent_id.encode() in response.content


def test_invalid_step_up_form_has_focusable_generic_error_summary(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = _step_up_client(rbac_graph)

    with runtime_role():
        response = client.post(
            "/auth/step-up/",
            {"otp_device": device.persistent_id, "otp_token": "not-a-code"},
        )

    parser = _StepUpParser()
    parser.feed(response.content.decode())
    summary = parser.attributes_by_id["auth-errors"]
    token = parser.attributes_by_id["id_otp_token"]
    assert response.status_code == 200
    assert summary["role"] == "alert"
    assert summary["tabindex"] == "-1"
    assert summary["data-focus-error"] is None
    assert token["aria-describedby"] == "otp-help id_otp_token_error"
    assert b"not-a-code" not in response.content


def test_step_up_route_has_specific_document_metadata(
    rbac_graph: RbacGraph,
) -> None:
    create_totp_device(rbac_graph.physician, confirmed=True)
    client = _step_up_client(rbac_graph)

    with runtime_role():
        response = client.get("/auth/step-up/")

    assert (
        b"Confirm a recent authenticator code before a sensitive Clinic OS action."
        in response.content
    )
    assert b"Confirm sensitive action \xc2\xb7 Clinic OS" in response.content
