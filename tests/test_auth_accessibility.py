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


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append((tag, dict(attrs)))

    def attributes_for(self, element_id: str) -> dict[str, str | None]:
        for _tag, attributes in self.elements:
            if attributes.get("id") == element_id:
                return attributes
        pytest.fail(f"missing element #{element_id}")

    def assert_descriptions_exist(self) -> None:
        ids = {
            element_id
            for _tag, attributes in self.elements
            if (element_id := attributes.get("id")) is not None
        }
        for _tag, attributes in self.elements:
            described_by = attributes.get("aria-describedby")
            if described_by is not None:
                assert set(described_by.split()) <= ids


def _parse(response_content: bytes) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(response_content.decode())
    return parser


def _logged_in_client(graph: RbacGraph) -> Client:
    client = Client()
    username = User.objects.get(pk=graph.physician).username
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
    return client


def test_bound_login_errors_have_real_descriptions_and_focus_target() -> None:
    response = Client().post("/auth/login/", {"username": "", "password": ""})
    parser = _parse(response.content)

    assert response.status_code == 200
    assert parser.attributes_for("id_username")["aria-describedby"] == (
        "login-help id_username_error"
    )
    assert parser.attributes_for("id_password")["aria-describedby"] == (
        "login-help id_password_error"
    )
    summary = parser.attributes_for("auth-errors")
    assert summary["data-focus-error"] is None
    assert summary["autofocus"] is None
    parser.assert_descriptions_exist()


def test_multi_device_selector_is_visibly_labeled_and_errors_are_described(
    rbac_graph: RbacGraph,
) -> None:
    first = create_totp_device(rbac_graph.physician, confirmed=True)
    create_totp_device(rbac_graph.physician, confirmed=True)
    client = _logged_in_client(rbac_graph)

    with runtime_role():
        default_response = client.get("/auth/verify/")
        error_response = client.post(
            "/auth/verify/",
            {"otp_device": first.persistent_id, "otp_token": "not-a-code"},
        )

    assert b'<label for="id_otp_device">Authenticator:</label>' in (
        default_response.content
    )
    assert b'<select name="otp_device"' in default_response.content
    parser = _parse(error_response.content)
    assert parser.attributes_for("id_otp_device")["aria-describedby"] == (
        "otp-device-help"
    )
    assert parser.attributes_for("id_otp_token")["aria-describedby"] == (
        "otp-help id_otp_token_error"
    )
    assert parser.attributes_for("auth-errors")["data-focus-error"] is None
    parser.assert_descriptions_exist()


def test_bound_device_error_references_a_rendered_generic_error(
    rbac_graph: RbacGraph,
) -> None:
    create_totp_device(rbac_graph.physician, confirmed=True)
    create_totp_device(rbac_graph.physician, confirmed=True)
    client = _logged_in_client(rbac_graph)

    with runtime_role():
        response = client.post(
            "/auth/verify/",
            {
                "otp_device": "otp_totp.totpdevice/999999999",
                "otp_token": "not-a-code",
            },
        )

    parser = _parse(response.content)
    assert parser.attributes_for("id_otp_device")["aria-describedby"] == (
        "otp-device-help id_otp_device_error"
    )
    assert b"999999999" not in response.content
    parser.assert_descriptions_exist()
