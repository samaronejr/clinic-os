"""New governance services preserve factor policy and exact tenant boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.enterprise import GovernanceDeniedError, configure_connection
from apps.identity.models import Organization
from django.test import override_settings

from auth.passkey_test_support import passkey_login, registered_passkey
from identity.enterprise_support import enterprise_setup
from identity.lifecycle_test_support import session_key
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_configuration_cannot_reuse_another_tenants_scope(
    rbac_graph: RbacGraph,
) -> None:
    # Given a real admin verified for organization A.
    setup = enterprise_setup(rbac_graph)
    # When that actor names a clinic in organization B.
    with (
        permission_context(rbac_graph, rbac_graph.clinic_admin),
        pytest.raises(GovernanceDeniedError),
    ):
        configure_connection(
            clinic_id=rbac_graph.clinic_c,
            issuer="https://synthetic-idp.invalid",
            client_id="synthetic-forged",
            session_key=setup.session_key,
        )
    # Then neither a caller selector nor an existing fresh factor adds authority.


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_browser_freshness_without_a_persisted_factor_is_not_authority(
    rbac_graph: RbacGraph,
) -> None:
    # Given an admin whose browser claims freshness but has no corresponding key.
    setup = enterprise_setup(rbac_graph)
    # When that unregistered session is used at the service boundary.
    with (
        permission_context(rbac_graph, rbac_graph.clinic_admin),
        pytest.raises(GovernanceDeniedError),
    ):
        configure_connection(
            clinic_id=rbac_graph.clinic_a,
            issuer="https://synthetic-idp.invalid",
            client_id="synthetic-forged",
            session_key=f"{setup.session_key}-unregistered",
        )
    # Then governance does not trust caller-side OTP/passkey claims.


@override_settings(
    CLINIC_ENTERPRISE_SYNTHETIC=True,
    WEBAUTHN_RP_ID="testserver",
    WEBAUTHN_ORIGIN="http://testserver",
)
@pytest.mark.parametrize("enabled", [False, True])
def test_governance_obeys_actual_passkey_substitution_policy(
    rbac_graph: RbacGraph, *, enabled: bool
) -> None:
    # Given an actual signed passkey session and an explicit organization policy.
    enterprise_setup(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        Organization.objects.filter(pk=rbac_graph.organization_a).update(
            passkey_step_up_enabled=enabled
        )
    client = passkey_login(registered_passkey(rbac_graph))
    # When that factor is presented to the governance service itself.
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        if enabled:
            configured = configure_connection(
                clinic_id=rbac_graph.clinic_a,
                issuer="https://synthetic-idp.invalid",
                client_id="synthetic-passkey",
                session_key=session_key(client),
            )
            # Then only an explicitly enabled substitution can configure SSO.
            assert configured.clinic_id == rbac_graph.clinic_a
        else:
            with pytest.raises(GovernanceDeniedError):
                configure_connection(
                    clinic_id=rbac_graph.clinic_a,
                    issuer="https://synthetic-idp.invalid",
                    client_id="synthetic-passkey",
                    session_key=session_key(client),
                )
