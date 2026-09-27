"""Todo 26's exact staff action matrix through has_permission, not role guesses."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import RoleGrant, User, UserClinicRole
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ACTIONS = ("tasks.view", "tasks.assign", "tasks.complete", "tasks.reassign")
MANAGERS = {"owner", "org_admin", "clinic_admin", "clinic_manager"}


@pytest.mark.parametrize(
    "role",
    [*UserClinicRole.Role.values, "patient_delegate", "support", "service_principal"],
)
def test_every_task_permission_for_every_actor_role(
    rbac_graph: RbacGraph, role: str
) -> None:
    graph = rbac_graph
    actor, _enrollment = permission_actor(graph, role)
    expected = set(ACTIONS[:3]) if role in UserClinicRole.Role.values else set()
    if role in MANAGERS:
        expected.add("tasks.reassign")
    with permission_context(graph, actor):
        for permission in ACTIONS:
            if permission in expected:
                assert require_permission(permission, clinic_id=graph.clinic_a) == actor
            else:
                with pytest.raises(CurrentActorError):
                    require_permission(permission, clinic_id=graph.clinic_a)
            for clinic in (graph.clinic_b, graph.clinic_c):
                with pytest.raises(CurrentActorError):
                    require_permission(permission, clinic_id=clinic)
    User.objects.filter(pk=actor).update(is_active=False)
    with permission_context(graph, actor):
        for permission in ACTIONS:
            with pytest.raises(CurrentActorError):
                require_permission(permission, clinic_id=graph.clinic_a)


def test_bundle_removal_blocks_task_permission_without_changing_other_actions(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role="physician",
            permission="tasks.complete",
            bundle_version=2,
            valid_from=timezone.now() - timedelta(days=1),
        )
    with permission_context(graph, graph.physician):
        assert (
            require_permission("tasks.view", clinic_id=graph.clinic_a)
            == graph.physician
        )
        with pytest.raises(CurrentActorError):
            require_permission("tasks.complete", clinic_id=graph.clinic_a)
