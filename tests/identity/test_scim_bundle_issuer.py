"""Directory mapping can bind an owner-issued bundle but cannot issue v2."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.enterprise import approve_group_mapping
from apps.identity.models import ScimRoleBinding, UserClinicRole
from apps.identity.scim import create_resource, scim_context
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from psycopg.errors import CheckViolation

from identity.enterprise_support import enterprise_setup
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_modern_mapping_requires_owner_issuance_and_never_revokes_native_authority(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": "synthetic-nurse", "externalId": "nurse-subject"},
        )
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={"displayName": "Synthetic nurses", "members": []},
        )
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        approve_group_mapping(
            clinic_id=rbac_graph.clinic_a,
            group_id=group.pk,
            unit_id=rbac_graph.clinic_a,
            bundle="nurse",
            session_key=setup.session_key,
        )
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
    assert response.status_code == 200
    assert user.user_id is not None
    with owner_context(rbac_graph.organization_a):
        assert not UserClinicRole.objects.filter(user_id=user.user_id).exists()
    # Clear the directory member before owner issuance so the next add must sync.
    with runtime_role():
        response = Client().patch(
            f"/scim/v2/Groups/{group.pk}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "members", "value": []}],
            },
            content_type="application/scim+json",
            HTTP_AUTHORIZATION=f"Bearer {setup.token}",
        )
    assert response.status_code == 200
    assert response.json()["members"] == []
    with owner_context(rbac_graph.organization_a):
        membership = UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=user.user_id,
            role="nurse",
            workflow_bundle_version=2,
        )
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
    assert response.status_code == 200
    with owner_context(rbac_graph.organization_a):
        binding = ScimRoleBinding.objects.get(membership=membership)
        assert not binding.manages_membership
    with runtime_role():
        response = Client().patch(
            f"/scim/v2/Groups/{group.pk}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "members", "value": []}],
            },
            content_type="application/scim+json",
            HTTP_AUTHORIZATION=f"Bearer {setup.token}",
        )
    assert response.status_code == 200
    with owner_context(rbac_graph.organization_a):
        membership.refresh_from_db()
        binding.refresh_from_db()
        assert membership.revoked_at is None
        assert membership.workflow_bundle_version == 2
        assert binding.revoked_at is not None
        revoked_at = binding.revoked_at
    # When a removed directory member is re-added to its independently issued role.
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
    # Then a fresh binding is appended and neither native authority nor history changes.
    assert response.status_code == 200
    with owner_context(rbac_graph.organization_a):
        membership.refresh_from_db()
        binding.refresh_from_db()
        assert membership.revoked_at is None
        assert membership.workflow_bundle_version == 2
        assert binding.revoked_at == revoked_at
        assert ScimRoleBinding.objects.filter(membership=membership).count() == 2
        current = ScimRoleBinding.objects.get(
            membership=membership, revoked_at__isnull=True
        )
        assert current.pk != binding.pk
        assert not current.manages_membership
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            ScimRoleBinding.objects.filter(pk=binding.pk).update(revoked_at=None)
        assert isinstance(refused.value.__cause__, CheckViolation)
