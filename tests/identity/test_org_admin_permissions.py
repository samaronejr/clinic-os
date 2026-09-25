"""Canonical org-admin scope composes with the retained all-clinic legacy gate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.clinic_configuration import CONFIGURATION_ROLES
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_org_admin,
    require_permission,
)
from apps.identity.models import Clinic, RoleGrant, User, UserClinicRole

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.current_context import ClinicRoles

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _all_clinics(graph: RbacGraph) -> UUID:
    actor, _ = permission_actor(graph, "org_admin")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=actor,
            role=UserClinicRole.Role.ORG_ADMIN,
        )
    return actor


@pytest.mark.parametrize(
    "role", ["owner", "clinic_admin", "physician", "receptionist", "org_admin"]
)
def test_one_clinic_never_confers_org_authority(
    rbac_graph: RbacGraph, role: str
) -> None:
    graph = rbac_graph
    actor, _ = permission_actor(graph, role)
    with permission_context(graph, actor), pytest.raises(CurrentActorError):
        require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)


@pytest.mark.parametrize(
    "roles", [CONFIGURATION_ROLES, (UserClinicRole.Role.ORG_ADMIN,)]
)
def test_canonical_org_admin_uses_remove_only_permissions(
    rbac_graph: RbacGraph,
    roles: ClinicRoles,
) -> None:
    graph = rbac_graph
    actor = _all_clinics(graph)
    with permission_context(graph, actor):
        assert require_current_actor_org_admin(graph.organization_a, roles) == actor
        assert (
            require_permission("staff.organization", clinic_id=graph.clinic_b) == actor
        )
        for organization in (graph.organization_b, uuid4()):
            with pytest.raises(CurrentActorError):
                require_current_actor_org_admin(organization, roles)
        with pytest.raises(CurrentActorError):
            require_current_actor_org_admin(graph.organization_a, ())
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            role=UserClinicRole.Role.ORG_ADMIN,
            permission="staff.organization",
            valid_from=datetime(2000, 1, 1, tzinfo=UTC),
        )
    with permission_context(graph, actor):
        assert (
            require_permission("staff.organization", clinic_id=graph.clinic_a) == actor
        )
        with pytest.raises(CurrentActorError):
            require_current_actor_org_admin(graph.organization_a, roles)


def test_org_admin_rechecks_membership_and_active_actor(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    actor = _all_clinics(graph)
    with permission_context(graph, actor):
        assert (
            require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)
            == actor
        )
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(user_id=actor, clinic_id=graph.clinic_b).delete()
    with permission_context(graph, actor), pytest.raises(CurrentActorError):
        require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=actor,
            role=UserClinicRole.Role.ORG_ADMIN,
        )
    User.objects.filter(pk=actor).update(is_active=False)
    with permission_context(graph, actor), pytest.raises(CurrentActorError):
        require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)


def test_new_clinic_requires_new_assignment(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    actor = _all_clinics(graph)
    with permission_context(graph, actor):
        assert (
            require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)
            == actor
        )
    with owner_context(graph.organization_a):
        Clinic.objects.create(
            organization_id=graph.organization_a,
            name="Sintetico Extra",
            timezone="America/Sao_Paulo",
        )
    with permission_context(graph, actor), pytest.raises(CurrentActorError):
        require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)


def test_mixed_legacy_and_canonical_assignments_cover_each_clinic(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor, _ = permission_actor(graph, "clinic_admin")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=actor,
            role=UserClinicRole.Role.ORG_ADMIN,
        )
    with permission_context(graph, actor):
        assert (
            require_current_actor_org_admin(graph.organization_a, CONFIGURATION_ROLES)
            == actor
        )
