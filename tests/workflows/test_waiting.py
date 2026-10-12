"""A task step gates later work until its real owner commits completion evidence."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.tenancy.db import tenant_context
from apps.workflows import engine
from apps.workflows.models import Task, WorkflowRun, WorkflowStep
from apps.workflows.services import cancel_task, complete_task, start_task
from django.utils import timezone

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.clock_support import database_clock
from workflows.permission_support import permission_actor
from workflows.test_engine import execute, publish, start, step_id, task_step

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_task_completion_gates_later_steps_and_survives_worker_reentry(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    owner, _enrollment = permission_actor(graph, "receptionist")
    run = start(
        graph, publish(graph, [task_step(), {"handler": "timer", "seconds": 0}])
    )
    first = step_id(graph, run)
    now = timezone.now()
    monkeypatch.setattr(engine, "utc_now", lambda: now)
    with database_clock(graph.organization_a, lambda: now) as tick:
        assert execute(first) == "waiting"
        with owner_context(graph.organization_a):
            step = WorkflowStep.objects.get(pk=first)
            assert step.created_task_id is not None
            task = Task.objects.get(pk=step.created_task_id)
            second = WorkflowStep.objects.get(run=run, position=1).pk
            assert task.state == "assigned"
            assert step.state == "waiting"
            assert WorkflowRun.objects.get(pk=run.pk).state == "waiting"
        assert execute(second) == "waiting"
        assert execute(first) == "waiting"
        with runtime_role(), tenant_context(owner, graph.organization_a):
            started = start_task(
                clinic_id=graph.clinic_a, task_id=task.pk, expected_revision=2
            )
            done = complete_task(
                clinic_id=graph.clinic_a,
                task_id=started.pk,
                expected_revision=3,
                evidence={"checked": True},
            )
            assert done.state == "done"
        now += timedelta(seconds=60)
        tick()
        assert execute(first) == "completed"
        assert execute(second) == "completed"
        assert execute(first) == "completed"
        with owner_context(graph.organization_a):
            assert Task.objects.count() == 1
            assert WorkflowRun.objects.get(pk=run.pk).state == "completed"
            task.refresh_from_db()
            assert task.completion_evidence == {"checked": True}


def test_cancelled_bound_task_fails_the_waiting_step_durably(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled bound task must persist a legal failed step and run outcome."""
    graph = rbac_graph
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    definition = publish(graph, [task_step(), {"handler": "timer", "seconds": 0}])
    run = start(graph, definition)
    first = step_id(graph, run)
    now = timezone.now()
    monkeypatch.setattr(engine, "utc_now", lambda: now)
    with database_clock(graph.organization_a, lambda: now) as tick:
        assert execute(first) == "waiting"
        with owner_context(graph.organization_a):
            step = WorkflowStep.objects.get(pk=first)
            assert step.created_task_id is not None
            task = Task.objects.get(pk=step.created_task_id)
            second = WorkflowStep.objects.get(run=run, position=1).pk
            assert task.state == "assigned"
            assert step.state == "waiting"
            assert WorkflowRun.objects.get(pk=run.pk).state == "waiting"
        with runtime_role(), tenant_context(manager, graph.organization_a):
            cancelled = cancel_task(
                clinic_id=graph.clinic_a,
                task_id=task.pk,
                expected_revision=task.revision,
            )
            assert cancelled.state == "cancelled"
        now += timedelta(seconds=60)
        tick()
        assert execute(first) == "failed"
        with owner_context(graph.organization_a):
            step.refresh_from_db()
            assert step.state == "failed"
            assert step.error_code == "operation_failed"
            assert step.fencing_token == 2
            assert step.claim_until is None
            assert step.wake_at is None
            assert WorkflowRun.objects.get(pk=run.pk).state == "failed"
        assert execute(second) == "cancelled"
        now += timedelta(seconds=60)
        tick()
        assert execute(first) == "failed"
        assert execute(second) == "cancelled"
        with owner_context(graph.organization_a):
            step.refresh_from_db()
            assert step.fencing_token == 2
            successor = WorkflowStep.objects.get(run=run, position=1)
            assert successor.state == "pending"
            assert successor.fencing_token == 0
            assert Task.objects.count() == 1
            task.refresh_from_db()
            assert task.state == "cancelled"
            assert WorkflowRun.objects.get(pk=run.pk).state == "failed"
            assert run.definition_version_id == definition.pk
