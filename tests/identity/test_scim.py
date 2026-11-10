"""SCIM provisioning uses real roles, retained history and approved mappings."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.identity.enterprise import approve_group_mapping, revoke_scim_token
from apps.identity.models import (
    ScimResource,
    ScimToken,
    SessionRecord,
    User,
    UserClinicRole,
)
from apps.identity.scim import create_resource, resource_document, scim_context
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
SEARCH = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:SearchRequest"]}


def _search(client: Client, token: str) -> _MonkeyPatchedWSGIResponse:
    return client.post(
        "/scim/v2/Users/.search",
        data=SEARCH,
        content_type="application/scim+json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_provisioning_commits_audit_with_credential_principal(
    rbac_graph: RbacGraph,
) -> None:
    # Given an authenticated directory credential, not its human issuer.
    setup = enterprise_setup(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        credential_id = setup.connection.tokens.get().pk
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        # When provisioning reaches the real audit boundary without a staff actor.
        resource = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-audited-subject"},
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT NULLIF(current_setting('app.current_user_id',true),'')"
            )
            assert cursor.fetchone() == (None,)
    # Then the resource and its attributable, metadata-only audit commit together.
    with owner_context(rbac_graph.organization_a):
        assert ScimResource.objects.filter(pk=resource.pk).exists()
        event = AuditEvent.objects.get(
            organization_id=rbac_graph.organization_a,
            event_type="identity.scim.resource_created",
            affected_record_id=str(resource.pk),
        )
        assert event.actor_user_id == credential_id
        assert event.actor_user_id != rbac_graph.clinic_admin
        assert event.payload == {
            "clinic_id": str(rbac_graph.clinic_a),
            "object_verb": "created",
        }
        assert verify_chain(rbac_graph.organization_a).valid


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_directory_deactivation_revokes_managed_roles_and_existing_sessions(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-deprovision", "active": True},
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
    assert user.user_id is not None
    verified_actor_client(user.user_id)
    with owner_context(rbac_graph.organization_a):
        assert SessionRecord.objects.filter(
            user_id=user.user_id, revoked_at__isnull=True
        ).exists()
    with runtime_role():
        response = Client().patch(
            f"/scim/v2/Users/{user.pk}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            content_type="application/scim+json",
            HTTP_AUTHORIZATION=f"Bearer {setup.token}",
        )
    assert response.status_code == 200
    with owner_context(rbac_graph.organization_a):
        assert not User.objects.get(pk=user.user_id).is_active
        assert not UserClinicRole.objects.filter(
            user_id=user.user_id, revoked_at__isnull=True
        ).exists()
        assert not SessionRecord.objects.filter(
            user_id=user.user_id, revoked_at__isnull=True
        ).exists()


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_approved_group_provisions_only_its_exact_unit_bundle(
    rbac_graph: RbacGraph,
) -> None:
    # Given an active token and a group approved by a real verified org admin.
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={
                "userName": "synthetic-staff",
                "externalId": "synthetic-subject",
                "active": True,
            },
        )
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={"displayName": "Synthetic reception", "members": []},
        )
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        approve_group_mapping(
            clinic_id=rbac_graph.clinic_a,
            group_id=group.pk,
            unit_id=rbac_graph.clinic_a,
            bundle="receptionist",
            session_key=setup.session_key,
        )
    # When the SCIM client adds that user to the mapped group.
    with runtime_role():
        response = Client().patch(
            f"/scim/v2/Groups/{group.pk}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": str(user.pk)}]}
                ],
            },
            content_type="application/scim+json",
            HTTP_AUTHORIZATION=f"Bearer {setup.token}",
        )
    # Then canonical membership is granted only in the approved clinic and role.
    assert response.status_code == 200
    assert user.user_id is not None
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        roles = set(
            UserClinicRole.objects.filter(user_id=user.user_id).values_list(
                "clinic_id", "role"
            )
        )
    assert roles == {(rbac_graph.clinic_a, "receptionist")}


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_revoked_scim_token_is_unauthorized(rbac_graph: RbacGraph) -> None:
    # Given a token revoked through the verified admin boundary.
    setup = enterprise_setup(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        token_id = setup.connection.tokens.get().pk
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        revoke_scim_token(
            clinic_id=rbac_graph.clinic_a,
            token_id=token_id,
            session_key=setup.session_key,
        )
    # When that bearer attempts a directory search.
    with runtime_role():
        response = Client().post(
            "/scim/v2/Users/.search",
            data={"schemas": ["urn:ietf:params:scim:api:messages:2.0:SearchRequest"]},
            content_type="application/scim+json",
            HTTP_AUTHORIZATION=f"Bearer {setup.token}",
        )
    # Then no session cookie or directory data is returned.
    assert response.status_code == 401
    assert not response.cookies
    assert response.json()["status"] == "401"


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_scim_budget_refuses_past_one_hundred_then_revocation_is_unauthorized(
    rbac_graph: RbacGraph,
) -> None:
    # Given a bearer that spent 99 of its 100 requests in the current window,
    # pinned ahead of the database clock so the window cannot roll over here.
    setup = enterprise_setup(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        token_id = setup.connection.tokens.get().pk
        pinned = ScimToken.objects.filter(pk=token_id).update(
            window_start=timezone.now() + timedelta(days=1), window_count=99
        )
    assert pinned == 1
    client = Client()
    # When its hundredth and hundred-and-first requests arrive.
    with runtime_role():
        last = _search(client, setup.token)
        limited = _search(client, setup.token)
    # Then the hundredth is answered and the next is refused with Retry-After,
    # reflecting neither the bearer nor a session.
    assert last.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert setup.token.encode("ascii") not in limited.content
    assert not limited.cookies
    # And once revoked, the same bearer is unauthorized inside its window.
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        revoke_scim_token(
            clinic_id=rbac_graph.clinic_a,
            token_id=token_id,
            session_key=setup.session_key,
        )
    with runtime_role():
        revoked = _search(client, setup.token)
    assert revoked.status_code == 401


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_foreign_and_unknown_scim_resources_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    # Given a valid token scoped to organization A.
    setup = enterprise_setup(rbac_graph)
    # When it addresses arbitrary unknown or clinical identifiers.
    client = Client()
    with runtime_role():
        responses = [
            client.get(
                f"/scim/v2/Users/{identifier}",
                HTTP_AUTHORIZATION=f"Bearer {setup.token}",
            )
            for identifier in (uuid4(), rbac_graph.clinic_c, rbac_graph.physician)
        ]
    # Then the same empty refusal is used without exposing identity existence.
    assert {response.status_code for response in responses} == {404}
    assert len({response.content for response in responses}) == 1


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_directory_resource_id_is_not_the_authentication_user_id(
    rbac_graph: RbacGraph,
) -> None:
    # Given an authenticated synthetic SCIM client.
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        # When a staff directory resource is provisioned.
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-id", "externalId": "synthetic-id"},
        )
        document = resource_document(
            clinic_id=scope.clinic_id, resource_id=user.pk, kind="Users"
        )
        # Then its URL identifier is a fresh UUID4, not a clinical or account id.
        identifier = document["id"]
        assert isinstance(identifier, str)
        assert UUID(identifier).version == 4
        assert user.pk != user.user_id
        assert ScimResource.objects.filter(pk=user.pk).exists()
