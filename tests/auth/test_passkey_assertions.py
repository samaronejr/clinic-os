"""Real signed assertions, session inventory and current organization policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import Organization, SessionRecord, User
from apps.identity.session_inventory import session_hash
from apps.identity.stepup import StepUpRequired, assert_step_up
from apps.tenancy.db import tenant_context
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory

from auth.passkey_test_support import (
    SoftwareAuthenticator,
    passkey_login,
    registered_passkey,
)
from identity.lifecycle_test_support import session_key, verified_client
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


def test_signed_discoverable_login_rotates_and_inventories_the_session(
    rbac_graph: RbacGraph,
) -> None:
    # Given a registered own credential with a genuine software public key.
    enrolled = verified_client(rbac_graph)
    authenticator = SoftwareAuthenticator.create()
    with runtime_role():
        options = enrolled.post(
            "/auth/passkey/register/begin/",
            {"clinic_id": str(rbac_graph.clinic_b)},
        ).json()
        created = enrolled.post(
            "/auth/passkey/register/finish/",
            {
                "credential": authenticator.registration(
                    challenge=options["challenge"], rp_id="testserver"
                ),
            },
        )
    assert created.status_code == 201
    client = Client()
    # When an anonymous session submits a real user-verified, signed assertion.
    with runtime_role():
        begin = client.post(
            "/auth/passkey/assert/begin/",
            {"purpose": "login", "next": "/auth/protected/"},
        )
    assert begin.status_code == 200
    old_key = session_key(client)
    with runtime_role():
        finish = client.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": authenticator.assertion(
                    challenge=begin.json()["challenge"],
                    user_handle=options["user"]["id"],
                ),
            },
        )
    # Then only the verified account is logged in and the old key is replaced.
    assert finish.status_code == 200
    assert client.session["_auth_user_id"] == str(rbac_graph.clinic_admin)
    assert client.session["active_org_id"] == str(rbac_graph.organization_a)
    assert session_key(client) != old_key
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        record = SessionRecord.objects.get(
            session_key_hash=session_hash(session_key(client))
        )
        assert record.verification_passkey_id is not None
        assert record.verification_device_id is None
        assert record.verified_at is not None


@pytest.mark.parametrize("enabled", [False, True])
def test_sensitive_boundary_accepts_passkey_only_when_org_policy_enables_it(
    rbac_graph: RbacGraph, *, enabled: bool
) -> None:
    # Given the explicit organization policy and an actual signed passkey session.
    with owner_context(rbac_graph.organization_a):
        Organization.objects.filter(pk=rbac_graph.organization_a).update(
            passkey_step_up_enabled=enabled
        )
    client = passkey_login(registered_passkey(rbac_graph))
    request = RequestFactory().get("/sensitive-action/")
    request.session = SessionStore(session_key=session_key(client))
    request.user = User.objects.get(pk=rbac_graph.clinic_admin)
    # When an operation calls the existing, role-neutral step-up boundary.
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        # Then TOTP substitution is impossible until that organization opts in.
        if enabled:
            assert_step_up(request)
        else:
            with pytest.raises(StepUpRequired):
                assert_step_up(request)
