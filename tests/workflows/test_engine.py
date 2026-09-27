"""Todo 26: immutable definitions, transactional effects and fenced reclaim."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows import engine
from apps.workflows.models import (
    Task,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowStep,
)
from apps.workflows.services import publish_definition, start_run
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def publish(
    graph: RbacGraph, steps: list[dict[str, object]]
) -> WorkflowDefinitionVersion:
    with owner_context(graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.shared_user,
            role=UserClinicRole.Role.CLINIC_MANAGER,
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        return publish_definition(
            clinic_id=graph.clinic_a,
            key="daily-check",
            steps=steps,
            idempotency_key=uuid4(),
        )


def start(graph: RbacGraph, definition: WorkflowDefinitionVersion) -> WorkflowRun:
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        return start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[{"kind": "clinic", "id": str(graph.clinic_a)}],
            idempotency_key=uuid4(),
        )


def step_id(graph: RbacGraph, run: WorkflowRun) -> UUID:
    with owner_context(graph.organization_a):
        return WorkflowStep.objects.get(run=run, position=0).pk


def task_step() -> dict[str, object]:
    return {
        "handler": "task",
        "kind": "checklist",
        "subject": 0,
        "due_seconds": 3600,
        "owner_role": "receptionist",
    }


def execute(identifier: UUID) -> str:
    try:
        with runtime_role():
            return engine.execute_step(step_id=identifier)
    finally:
        connections.close_all()


def test_published_definition_and_run_pin_are_immutable(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    first = publish(graph, [task_step()])
    run = start(graph, first)
    second = publish(graph, [{"handler": "timer", "seconds": 120}])
    assert second.version == first.version + 1
    with owner_context(graph.organization_a):
        run.refresh_from_db()
        assert run.definition_version_id == first.pk
        for model, identifier, change in (
            (WorkflowDefinitionVersion, first.pk, {"steps": []}),
            (WorkflowRun, run.pk, {"definition_version_id": second.pk}),
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.filter(pk=identifier).update(**change)
    assert execute(step_id(graph, run)) == "completed"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 1


def test_live_claim_cannot_be_reclaimed_even_after_lease_deadline(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    run = start(graph, publish(graph, [task_step()]))
    identifier = step_id(graph, run)
    ready = Barrier(2, timeout=15)
    finish = Event()
    now = timezone.now()
    monkeypatch.setattr(engine, "utc_now", lambda: now)

    apply = engine.apply_claim

    def pause(*, step_id: UUID, fencing_token: int) -> engine.Result:
        ready.wait()
        assert finish.wait(15)
        return apply(step_id=step_id, fencing_token=fencing_token)

    monkeypatch.setattr(engine, "apply_claim", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(execute, identifier)
        try:
            ready.wait()
            now += timedelta(hours=1)
            assert execute(identifier) == "busy"
        finally:
            finish.set()
        assert first.result(timeout=15) == "completed"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 1
        step = WorkflowStep.objects.get(pk=identifier)
        assert step.fencing_token == 1
        assert step.state == "completed"


def test_old_fencing_token_cannot_apply_effect(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    run = start(graph, publish(graph, [task_step()]))
    identifier = step_id(graph, run)
    now = timezone.now()
    monkeypatch.setattr(engine, "utc_now", lambda: now)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        first = engine.claim_step(step_id=identifier)
    now += timedelta(hours=1)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        second = engine.claim_step(step_id=identifier)
        assert first == 1
        assert second == 2
        assert engine.apply_claim(step_id=identifier, fencing_token=first) == "stale"
        assert Task.objects.count() == 0
        assert (
            engine.apply_claim(step_id=identifier, fencing_token=second) == "completed"
        )
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 1


def test_timer_clock_and_scanner_no_early_execution(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    now = timezone.now()
    monkeypatch.setattr(engine, "utc_now", lambda: now)
    run = start(
        graph, publish(graph, [{"handler": "timer", "seconds": 120}, task_step()])
    )
    identifier = step_id(graph, run)
    assert execute(identifier) == "waiting"
    now += timedelta(seconds=119)
    assert execute(identifier) == "waiting"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 0
    now += timedelta(seconds=1)
    assert execute(identifier) == "completed"
    with owner_context(graph.organization_a):
        next_id = WorkflowStep.objects.get(run=run, position=1).pk
    assert execute(next_id) == "completed"
    with owner_context(graph.organization_a):
        run.refresh_from_db()
        assert run.state == "completed"
        assert Task.objects.count() == 1


@pytest.mark.parametrize("revocation", ["inactive", "membership"])
def test_queued_step_does_not_run_for_revoked_actor(
    rbac_graph: RbacGraph, revocation: str
) -> None:
    graph = rbac_graph
    run = start(graph, publish(graph, [task_step()]))
    identifier = step_id(graph, run)
    if revocation == "inactive":
        User.objects.filter(pk=graph.shared_user).update(is_active=False)
    else:
        with owner_context(graph.organization_a):
            UserClinicRole.objects.filter(
                user_id=graph.shared_user, clinic_id=graph.clinic_a
            ).delete()
    assert execute(identifier) == "denied"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 0
        step = WorkflowStep.objects.get(pk=identifier)
        assert step.fencing_token == 0
        assert step.state == "pending"
