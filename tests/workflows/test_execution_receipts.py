"""A handler result alone cannot certify a committed workflow effect."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from apps.workflows import engine
from apps.workflows.models import Task, WorkflowRun, WorkflowStep

from identity.permission_support import owner_context
from workflows.test_engine import execute, publish, start, step_id, task_step

if TYPE_CHECKING:
    from datetime import datetime

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_task_handler_without_a_stored_effect_fails(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an admitted run and a broken handler reporting success without work.
    graph = rbac_graph
    run = start(graph, publish(graph, [task_step()]))
    identifier = step_id(graph, run)

    def no_effect(_step: WorkflowStep, _now: datetime) -> Literal["completed"]:
        return "completed"

    monkeypatch.setitem(engine.HANDLERS, "task", no_effect)
    # When: the actual fenced executor applies that handler.
    result = execute(identifier)
    # Then: it records failure instead of certifying a nonexistent task.
    assert result == "failed"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 0
        assert WorkflowRun.objects.get(pk=run.pk).state == "failed"
        assert WorkflowStep.objects.get(pk=identifier).error_code == "handler_rejected"
