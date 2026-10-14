"""Native own-credential, policy and authenticator retirement regressions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import Organization, Passkey, UserClinicRole
from apps.identity.recovery import issue_recovery_codes, recover_authenticator
from apps.tenancy.db import tenant_context

from auth.passkey_test_support import passkey_login, registered_passkey
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


def test_own_native_page_exposes_registration_and_code_fallback(
    rbac_graph: RbacGraph,
) -> None:
    # Given a real verified session.
    client = verified_client(rbac_graph)
    # When the user opens their account credential page.
    with runtime_role():
        response = client.get("/account/passkeys/")
    # Then registration is enhanced, while the code path remains native.
    assert response.status_code == 200
    assert b'data-passkey="register"' in response.content
    assert b'href="/auth/step-up/' in response.content


def test_own_removal_rejects_a_bound_passkey_session_on_its_next_request(
    rbac_graph: RbacGraph,
) -> None:
    # Given one credential and a separate genuinely verified passkey session.
    registered = registered_passkey(rbac_graph)
    signed = passkey_login(registered)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        passkey = Passkey.objects.get(user_id=rbac_graph.clinic_admin)
    # When the owner removes it through the native, body-only form.
    with runtime_role():
        response = registered.client.post(
            "/account/passkeys/remove/",
            {"clinic_id": str(rbac_graph.clinic_b), "passkey_id": str(passkey.pk)},
        )
        reused = signed.get("/auth/protected/")
    # Then removal is durable and its existing signed session is immediately refused.
    assert response.status_code == 302
    assert reused.status_code == 302
    assert reused.headers["Location"].startswith("/auth/login/")
    with owner_context(rbac_graph.organization_a):
        assert Passkey.objects.get(pk=passkey.pk).removed_at is not None


def test_org_policy_requires_fresh_org_admin_authority_in_every_unit(
    rbac_graph: RbacGraph,
) -> None:
    # Given genuine org_admin coverage in both units before authentication.
    with owner_context(rbac_graph.organization_a):
        for clinic_id in (rbac_graph.clinic_a, rbac_graph.clinic_b):
            UserClinicRole.objects.create(
                user_id=rbac_graph.clinic_admin,
                organization_id=rbac_graph.organization_a,
                clinic_id=clinic_id,
                role=UserClinicRole.Role.ORG_ADMIN,
            )
    client = verified_client(rbac_graph)
    # When that administrator explicitly enables passkey substitution.
    with runtime_role():
        response = client.post(
            "/account/passkeys/policy/",
            {"clinic_id": str(rbac_graph.clinic_b), "enabled": "on"},
        )
    # Then the organization's default-off policy changes, not another tenant's.
    assert response.status_code == 302
    with owner_context(rbac_graph.organization_a):
        assert Organization.objects.get(
            pk=rbac_graph.organization_a
        ).passkey_step_up_enabled
    with owner_context(rbac_graph.organization_b):
        assert not Organization.objects.get(
            pk=rbac_graph.organization_b
        ).passkey_step_up_enabled


def test_recovery_retires_passkeys_as_well_as_old_totp_and_sessions(
    rbac_graph: RbacGraph,
) -> None:
    # Given an actual registered credential and once-issued recovery enrollment.
    registered = registered_passkey(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        codes = issue_recovery_codes(
            clinic_id=rbac_graph.clinic_b,
            session_key=session_key(registered.client),
        )
        # When one real recovery code is consumed through the existing service.
        assert recover_authenticator(clinic_id=rbac_graph.clinic_b, code=codes.codes[0])
        # Then no old passkey remains an active authentication route.
        assert not Passkey.objects.filter(
            user_id=rbac_graph.clinic_admin, removed_at__isnull=True
        ).exists()
