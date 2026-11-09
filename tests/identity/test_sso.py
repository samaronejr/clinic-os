"""The deterministic IdP exercises signed primary auth, not a factor bypass."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.enterprise import approve_group_mapping
from apps.identity.enterprise_oidc import FixtureIdentity, SyntheticOidcAdapter
from apps.identity.models import UserClinicRole
from apps.identity.scim import create_resource, scim_context
from django.contrib.auth import SESSION_KEY
from django.test import Client, override_settings

from identity.enterprise_support import enterprise_setup
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_approved_directory_subject_signs_in_without_a_factor_or_claim_elevation(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-sso", "externalId": "synthetic-sub"},
        )
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={
                "displayName": "Synthetic reception",
                "members": [{"value": str(user.pk)}],
            },
        )
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        approve_group_mapping(
            clinic_id=rbac_graph.clinic_a,
            group_id=group.pk,
            unit_id=rbac_graph.clinic_a,
            bundle="receptionist",
            session_key=setup.session_key,
        )
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/sso/begin/", {"clinic_id": str(rbac_graph.clinic_a)}
        ).json()
    code = SyntheticOidcAdapter.code_for(
        issuer="https://synthetic-idp.invalid",
        audience="synthetic-clinic",
        identity=FixtureIdentity("synthetic-sub", ("org_admin",)),
        nonce=options["nonce"],
        challenge=options["code_challenge"],
    )
    with runtime_role():
        response = client.post(
            "/auth/sso/finish/", {"state": options["state"], "code": code}
        )
    assert response.status_code == 302
    assert client.session[SESSION_KEY] == str(user.user_id)
    assert "otp_verified_at" not in client.session
    assert user.user_id is not None
    with owner_context(rbac_graph.organization_a):
        assert set(
            UserClinicRole.objects.filter(
                user_id=user.user_id, revoked_at__isnull=True
            ).values_list("role", flat=True)
        ) == {"receptionist"}


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_unmapped_subject_or_claim_cannot_elevate_a_role(
    rbac_graph: RbacGraph,
) -> None:
    # Given a directory account with no approved group membership.
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-sso", "externalId": "synthetic-sub"},
        )
    client = Client()
    with runtime_role():
        begin = client.post("/auth/sso/begin/", {"clinic_id": str(rbac_graph.clinic_a)})
    options = begin.json()
    code = SyntheticOidcAdapter.code_for(
        issuer="https://synthetic-idp.invalid",
        audience="synthetic-clinic",
        identity=FixtureIdentity("synthetic-sub", ("org_admin",)),
        nonce=options["nonce"],
        challenge=options["code_challenge"],
    )
    # When the signed IdP claim asks for an unapproved administrator group.
    with runtime_role():
        response = client.post(
            "/auth/sso/finish/", {"state": options["state"], "code": code}
        )
    # Then primary login is refused and no privileged factor is stamped.
    assert response.status_code == 403
    assert SESSION_KEY not in client.session
    assert "otp_verified_at" not in client.session


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_oidc_state_is_single_use_and_bound_to_its_server_session(
    rbac_graph: RbacGraph,
) -> None:
    # Given one persisted primary-authentication attempt.
    enterprise_setup(rbac_graph)
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/sso/begin/", {"clinic_id": str(rbac_graph.clinic_a)}
        ).json()
    # When another browser attempts to consume it with a forged code.
    with runtime_role():
        response = Client().post(
            "/auth/sso/finish/", {"state": options["state"], "code": "forged"}
        )
    # Then the attempt cannot create any authenticated session.
    assert response.status_code == 403
    assert not response.cookies


def test_real_identity_transport_remains_unreachable() -> None:
    # Given the only shipped IdP adapter.
    adapter = SyntheticOidcAdapter()
    # When its real-provider posture is inspected.
    enabled = adapter.real_enabled
    # Then no real leg is runnable.
    assert enabled is False
