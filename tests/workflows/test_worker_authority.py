"""Queued runs recheck every permission, including narrowed multi-role bundles."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import RoleGrant, UserClinicRole
from apps.identity.permissions import BUNDLES_V2
from apps.workflows.models import Task, WorkflowStep
from django.utils import timezone

from identity.permission_support import owner_context
from workflows.test_engine import execute, publish, start, step_id, task_step

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def remove_permission(graph: RbacGraph, permission: str) -> None:
    with owner_context(graph.organization_a):
        roles = UserClinicRole.objects.filter(
            user_id=graph.shared_user, clinic_id=graph.clinic_a
        ).values_list("role", flat=True)
        grants = [
            RoleGrant(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                role=role,
                permission=permission,
                bundle_version=2,
                valid_from=timezone.now() - timedelta(days=1),
            )
            for role in roles
            if permission in BUNDLES_V2[role]
        ]
        assert grants
        RoleGrant.objects.bulk_create(grants)


@pytest.mark.parametrize("permission", ["tasks.view", "tasks.assign", "tasks.reassign"])
@pytest.mark.parametrize("handler", ["timer", "task"])
def test_queued_step_refuses_narrowed_bundle(
    rbac_graph: RbacGraph, permission: str, handler: str
) -> None:
    graph = rbac_graph
    specification = (
        {"handler": "timer", "seconds": 0} if handler == "timer" else task_step()
    )
    run = start(graph, publish(graph, [specification]))
    identifier = step_id(graph, run)
    remove_permission(graph, permission)
    assert execute(identifier) == "denied"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 0
        step = WorkflowStep.objects.get(pk=identifier)
        assert step.fencing_token == 0
        assert step.state == "pending"
