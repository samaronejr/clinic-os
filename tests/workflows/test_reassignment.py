"""Bulk reassignment is previewed, actor-bound and all-or-nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from apps.workflows.reassignment import apply_reassignment, preview_reassignment
from apps.workflows.services import (
    TaskOwner,
    WorkflowConflictError,
    assign_task,
    create_task,
)

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.test_engine import publish, task_step
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_preview_is_read_only_and_apply_binds_exact_revisions(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    publish(graph, [task_step()])
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        tasks = [
            create_task(
                clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
            )
            for _ in range(2)
        ]
        preview = preview_reassignment(
            clinic_id=graph.clinic_a,
            task_ids=[task.pk for task in tasks],
            owner=TaskOwner(user_id=graph.physician),
        )
        assert [task.state for task in Task.objects.order_by("id")] == ["open", "open"]
        assert {task.pk for task in preview.tasks} == {task.pk for task in tasks}
        changed = apply_reassignment(
            clinic_id=graph.clinic_a, preview_token=preview.token
        )
        assert {task.owner_user_id for task in changed} == {graph.physician}
        assert {task.state for task in changed} == {"assigned"}
        with pytest.raises(WorkflowConflictError):
            apply_reassignment(clinic_id=graph.clinic_a, preview_token=preview.token)


def test_stale_or_tampered_preview_changes_no_tasks(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    publish(graph, [task_step()])
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        tasks = [
            create_task(
                clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
            )
            for _ in range(2)
        ]
        preview = preview_reassignment(
            clinic_id=graph.clinic_a,
            task_ids=[task.pk for task in tasks],
            owner=TaskOwner(user_id=graph.physician),
        )
        assign_task(
            clinic_id=graph.clinic_a,
            task_id=tasks[-1].pk,
            owner=TaskOwner(user_id=graph.shared_user),
            expected_revision=1,
        )
        for token in (preview.token, preview.token + "invalid"):
            with pytest.raises(WorkflowConflictError):
                apply_reassignment(clinic_id=graph.clinic_a, preview_token=token)
    with owner_context(graph.organization_a):
        tasks[0].refresh_from_db()
        tasks[-1].refresh_from_db()
        assert tasks[0].state == "open"
        assert tasks[-1].owner_user_id == graph.shared_user
