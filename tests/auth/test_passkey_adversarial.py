"""Signed adversarial wire responses, expiry and replay on the actual PG roles."""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import TYPE_CHECKING

import pytest
from apps.audit.models import AuditEvent
from apps.identity import passkey_challenges
from apps.identity.models import Passkey
from apps.tenancy.db import tenant_context
from django.test import Client
from webauthn.helpers import bytes_to_base64url

from auth.passkey_test_support import (
    AssertionOptions,
    passkey_login,
    registered_passkey,
)
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _webauthn_settings(settings: SettingsWrapper) -> None:
    settings.WEBAUTHN_RP_ID = "testserver"
    settings.WEBAUTHN_ORIGIN = "http://testserver"


@pytest.mark.parametrize(
    "parameters",
    [
        AssertionOptions(rp_id="foreign.example"),
        AssertionOptions(origin="https://foreign.example"),
        AssertionOptions(verified=False),
    ],
    ids=["wrong-rp", "wrong-origin", "missing-user-verification"],
)
def test_signed_wrong_binding_never_authenticates_or_advances_counter(
    rbac_graph: RbacGraph, parameters: AssertionOptions
) -> None:
    # Given a real public key but an authenticator signing the wrong binding.
    registered = registered_passkey(rbac_graph)
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
        # When genuinely signed bytes claim a foreign RP/origin or omit UV.
        response = client.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": registered.authenticator.assertion(
                    challenge=options["challenge"],
                    user_handle=registered.user_handle,
                    options=parameters,
                )
            },
        )
    # Then no candidate account or counter becomes authenticated.
    assert response.status_code == 403
    assert "_auth_user_id" not in client.session
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        assert Passkey.objects.get(user_id=rbac_graph.clinic_admin).sign_count == 0


def test_signed_foreign_user_handle_does_not_select_the_candidate_account(
    rbac_graph: RbacGraph,
) -> None:
    registered = registered_passkey(rbac_graph)
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
        response = client.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": registered.authenticator.assertion(
                    challenge=options["challenge"],
                    user_handle=bytes_to_base64url(
                        rbac_graph.organization_b.bytes + rbac_graph.shared_user.bytes
                    ),
                )
            },
        )
    assert response.status_code == 403
    assert "_auth_user_id" not in client.session


def test_verified_counter_regression_is_refused_and_audited_without_wire_data(
    rbac_graph: RbacGraph,
) -> None:
    # Given an actual prior assertion that advanced this credential to one.
    registered = registered_passkey(rbac_graph)
    passkey_login(registered)
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
        # When the same real key signs an equal counter in a new ceremony.
        response = client.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": registered.authenticator.assertion(
                    challenge=options["challenge"], user_handle=registered.user_handle
                )
            },
        )
    # Then it is not admitted, the counter stays one and the refusal is attributable.
    assert response.status_code == 403
    assert "_auth_user_id" not in client.session
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        passkey = Passkey.objects.get(user_id=rbac_graph.clinic_admin)
        assert passkey.sign_count == 1
    # Audit contents intentionally have no runtime table-read grant.
    with owner_context(rbac_graph.organization_a):
        event = AuditEvent.objects.get(
            event_type="identity.passkey.counter_regression",
            affected_record_id=passkey.pk,
        )
        assert set(event.payload).issubset(
            {
                "clinic_id",
                "http_method",
                "http_status",
                "object_verb",
                "reason_code",
                "request_id",
            }
        )


def test_expiry_is_server_bound_and_consumes_an_otherwise_valid_signature(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a newly issued server ceremony and a fully capable authenticator.
    registered = registered_passkey(rbac_graph)
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
    issued_at = passkey_challenges.now_seconds()
    monkeypatch.setattr(passkey_challenges, "now_seconds", lambda: issued_at + 301)
    # When the exact signature arrives after five minutes, without timer waits.
    with runtime_role():
        response = client.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": registered.authenticator.assertion(
                    challenge=options["challenge"], user_handle=registered.user_handle
                )
            },
        )
    assert response.status_code == 403
    assert passkey_challenges.CHALLENGE_KEY not in client.session


def test_zero_counter_device_is_supported_but_a_used_nonce_cannot_replay(
    rbac_graph: RbacGraph,
) -> None:
    registered = registered_passkey(rbac_graph)
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
    stale = Client()
    stale.cookies = SimpleCookie(client.cookies)
    credential = registered.authenticator.assertion(
        challenge=options["challenge"],
        user_handle=registered.user_handle,
        options=AssertionOptions(sign_count=0),
    )
    with runtime_role():
        first = client.post("/auth/passkey/assert/finish/", {"credential": credential})
        replayed = stale.post(
            "/auth/passkey/assert/finish/", {"credential": credential}
        )
    assert first.status_code == 200
    assert replayed.status_code == 403
    assert "_auth_user_id" not in stale.session


@pytest.mark.parametrize("raw", ["", "null", "[]", "{}", '{"response":{}}', "not-json"])
def test_malformed_assertion_is_closed_and_cannot_leave_a_live_challenge(
    rbac_graph: RbacGraph, raw: str
) -> None:
    registered_passkey(rbac_graph)
    client = Client()
    with runtime_role():
        client.post("/auth/passkey/assert/begin/", {"purpose": "login"})
        response = client.post("/auth/passkey/assert/finish/", {"credential": raw})
    assert response.status_code == 403
    assert "_auth_user_id" not in client.session
    assert passkey_challenges.CHALLENGE_KEY not in client.session
