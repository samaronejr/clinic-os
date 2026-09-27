"""Real records and independent permission expectations for every workflow guard."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.comms.adapters import OperationScope
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.identity.models import Clinic, User, UserClinicRole
from apps.workflows import (
    access,
    engine,
    external,
    reassignment,
    run_services,
    task_services,
    tasks,
    views,
)
from apps.workflows.models import (
    Task,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowStep,
)
from apps.workflows.validation import TaskOwner, TaskSpec
from django.http import HttpRequest, HttpResponseBase
from django.test import RequestFactory
from django.urls import resolve
from django.utils import timezone

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class GuardWorld:
    graph: RbacGraph
    actor: UUID
    open_task: Task
    assigned: Task
    progress: Task
    review: Task
    due: Task
    parent: Task
    definition: WorkflowDefinitionVersion
    run: WorkflowRun
    pending: WorkflowStep
    claimed: WorkflowStep
    task_step: WorkflowStep
    operation: IntegrationOperation
    scope: OperationScope
    preview: str
    request: HttpRequest

    @property
    def clinic(self) -> UUID:
        return self.graph.clinic_a

    def creation(self) -> TaskSpec:
        return TaskSpec(
            kind="checklist",
            subject_ref={"kind": "clinic", "id": str(self.clinic)},
            due_at=self.open_task.due_at,
            depends_on=self.parent.pk,
        )

    def post(self, **fields: str) -> HttpRequest:
        request = self.request
        request.method = "POST"
        request.POST = RequestFactory().post(request.path, data=fields).POST
        return request


@dataclass(frozen=True)
class GuardCase:
    symbol: str
    permission: str
    invoke: Callable[[GuardWorld], object]
    owns_transaction: bool = False
    predicate: bool = False
    transport: bool = False


def seed_guard_world(graph: RbacGraph, patch: pytest.MonkeyPatch) -> GuardWorld:
    actor = User.objects.create(username=f"sintetico-workflow-guard-{uuid4().hex}")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user=actor,
            role="clinic_manager",
        )
    dispatched: list[UUID] = []
    patch.setattr(integration, "_dispatch", dispatched.append)
    external.register_adapters()
    reference = {"kind": "clinic", "id": str(graph.clinic_a)}
    specification = TaskSpec(
        kind="checklist",
        subject_ref=reference,
        due_at=timezone.now() + timedelta(days=1),
    )
    with permission_context(graph, actor.pk):
        open_task = task_services.create_task(
            clinic_id=graph.clinic_a, spec=specification, idempotency_key=uuid4()
        )
        assigned = task_services.create_task(
            clinic_id=graph.clinic_a, spec=specification, idempotency_key=uuid4()
        )
        assigned = task_services.assign_task(
            clinic_id=graph.clinic_a,
            task_id=assigned.pk,
            owner=TaskOwner(user_id=actor.pk),
            expected_revision=1,
        )
        progress = task_services.create_task(
            clinic_id=graph.clinic_a, spec=specification, idempotency_key=uuid4()
        )
        progress = task_services.assign_task(
            clinic_id=graph.clinic_a,
            task_id=progress.pk,
            owner=TaskOwner(user_id=actor.pk),
            expected_revision=1,
        )
        progress = task_services.start_task(
            clinic_id=graph.clinic_a, task_id=progress.pk, expected_revision=2
        )
        review = task_services.create_task(
            clinic_id=graph.clinic_a,
            spec=replace(specification, kind="review"),
            idempotency_key=uuid4(),
        )
        review = task_services.assign_task(
            clinic_id=graph.clinic_a,
            task_id=review.pk,
            owner=TaskOwner(user_id=actor.pk),
            expected_revision=1,
        )
        review = task_services.start_task(
            clinic_id=graph.clinic_a, task_id=review.pk, expected_revision=2
        )
        due = task_services.create_task(
            clinic_id=graph.clinic_a,
            spec=replace(specification, due_at=timezone.now() - timedelta(days=1)),
            idempotency_key=uuid4(),
        )
        parent = task_services.create_task(
            clinic_id=graph.clinic_a, spec=specification, idempotency_key=uuid4()
        )
        parent = task_services.assign_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            owner=TaskOwner(user_id=actor.pk),
            expected_revision=1,
        )
        parent = task_services.start_task(
            clinic_id=graph.clinic_a, task_id=parent.pk, expected_revision=2
        )
        parent = task_services.complete_task(
            clinic_id=graph.clinic_a,
            task_id=parent.pk,
            evidence={"checked": True},
            expected_revision=3,
        )
        definition = run_services.publish_definition(
            clinic_id=graph.clinic_a,
            key="guard-timer",
            idempotency_key=uuid4(),
            steps=[{"handler": "timer", "seconds": 0}],
        )
        run = run_services.start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[reference],
            idempotency_key=uuid4(),
        )
        pending = WorkflowStep.objects.get(run=run)
        claimed_run = run_services.start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[reference],
            idempotency_key=uuid4(),
        )
        claimed = WorkflowStep.objects.get(run=claimed_run)
        assert engine.claim_step(step_id=claimed.pk) == 1
        task_definition = run_services.publish_definition(
            clinic_id=graph.clinic_a,
            key="guard-task",
            idempotency_key=uuid4(),
            steps=[
                {
                    "handler": "task",
                    "kind": "checklist",
                    "subject": 0,
                    "due_seconds": 60,
                    "owner_role": "nurse",
                }
            ],
        )
        task_run = run_services.start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=task_definition.pk,
            context_refs=[reference],
            idempotency_key=uuid4(),
        )
        task_step = WorkflowStep.objects.select_related("run__definition_version").get(
            run=task_run
        )
        external_definition = run_services.publish_definition(
            clinic_id=graph.clinic_a,
            key="guard-external",
            idempotency_key=uuid4(),
            steps=[{"handler": "external", "provider": "workflow-synthetic-v1"}],
        )
        external_run = run_services.start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=external_definition.pk,
            context_refs=[reference],
            idempotency_key=uuid4(),
        )
        external_step = WorkflowStep.objects.get(run=external_run)
        assert engine.claim_step(step_id=external_step.pk) == 1
        assert (
            engine.apply_claim(step_id=external_step.pk, fencing_token=1) == "waiting"
        )
        external_step.refresh_from_db()
        assert external_step.operation_id is not None
        operation = IntegrationOperation.objects.get(pk=external_step.operation_id)
        preview = reassignment.preview_reassignment(
            clinic_id=graph.clinic_a,
            task_ids=[open_task.pk],
            owner=TaskOwner(user_id=actor.pk),
        ).token
    request = verified_request(actor.pk, verified_at=STEP_UP_NOW)
    request.method = "GET"
    request.path = f"/clinics/{graph.clinic_a}/tasks/"
    request.path_info = request.path
    request.resolver_match = resolve(request.path)
    request.META.update(SERVER_NAME="testserver", SERVER_PORT="80")
    return GuardWorld(
        graph,
        actor.pk,
        open_task,
        assigned,
        progress,
        review,
        due,
        parent,
        definition,
        run,
        pending,
        claimed,
        task_step,
        operation,
        OperationScope(
            operation_id=operation.pk,
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            actor_id=actor.pk,
        ),
        preview,
        request,
    )


def guard_cases() -> tuple[GuardCase, ...]:
    return (
        GuardCase(
            "apps.workflows.access.owner_matches",
            "tasks.view",
            lambda w: access.owner_matches(w.assigned),
        ),
        GuardCase(
            "apps.workflows.access.require_clinic_access",
            "tasks.view",
            lambda w: access.require_clinic_access(
                clinic_id=w.clinic, permission="tasks.view"
            ),
        ),
        GuardCase(
            "apps.workflows.access.require_manager",
            "tasks.reassign",
            lambda w: access.require_manager(clinic_id=w.clinic),
        ),
        GuardCase(
            "apps.workflows.access.require_owner_target",
            "tasks.assign",
            lambda w: access.require_owner_target(
                clinic_id=w.clinic, user_id=w.actor, role=""
            ),
        ),
        GuardCase(
            "apps.workflows.access.require_reference",
            "tasks.view",
            lambda w: access.require_reference(
                clinic_id=w.clinic, value={"kind": "clinic", "id": str(w.clinic)}
            ),
        ),
        GuardCase(
            "apps.workflows.access.require_task_access",
            "tasks.view",
            lambda w: access.require_task_access(
                clinic_id=w.clinic,
                task_id=w.assigned.pk,
                permission="tasks.view",
                owned=True,
            ),
        ),
        GuardCase(
            "apps.workflows.engine._apply_or_fail",
            "tasks.reassign",
            lambda w: engine._apply_or_fail(w.claimed.pk, 1),
        ),
        GuardCase(
            "apps.workflows.engine._locked_step",
            "tasks.reassign",
            lambda w: engine._locked_step(w.pending.pk),
        ),
        GuardCase(
            "apps.workflows.engine._task",
            "tasks.reassign",
            lambda w: engine._task(w.task_step),
        ),
        GuardCase(
            "apps.workflows.engine.apply_claim",
            "tasks.reassign",
            lambda w: engine.apply_claim(step_id=w.claimed.pk, fencing_token=1),
        ),
        GuardCase(
            "apps.workflows.engine.claim_step",
            "tasks.reassign",
            lambda w: engine.claim_step(step_id=w.pending.pk),
        ),
        GuardCase(
            "apps.workflows.engine.execute_step",
            "tasks.reassign",
            lambda w: engine.execute_step(step_id=w.pending.pk),
            owns_transaction=True,
            transport=True,
        ),
        GuardCase(
            "apps.workflows.external.SyntheticWorkflowAdapter.prepare",
            "tasks.reassign",
            lambda w: external.SyntheticWorkflowAdapter().prepare(w.operation),
        ),
        GuardCase(
            "apps.workflows.external.step_send_eligible",
            "tasks.reassign",
            lambda w: external.step_send_eligible(w.scope),
            predicate=True,
        ),
        GuardCase(
            "apps.workflows.reassignment.apply_reassignment",
            "tasks.reassign",
            lambda w: reassignment.apply_reassignment(
                clinic_id=w.clinic, preview_token=w.preview
            ),
        ),
        GuardCase(
            "apps.workflows.reassignment.preview_reassignment",
            "tasks.reassign",
            lambda w: reassignment.preview_reassignment(
                clinic_id=w.clinic,
                task_ids=[w.open_task.pk],
                owner=TaskOwner(user_id=w.actor),
            ),
        ),
        GuardCase(
            "apps.workflows.run_services.cancel_run",
            "tasks.reassign",
            lambda w: run_services.cancel_run(clinic_id=w.clinic, run_id=w.run.pk),
        ),
        GuardCase(
            "apps.workflows.run_services.publish_definition",
            "tasks.reassign",
            lambda w: run_services.publish_definition(
                clinic_id=w.clinic,
                key="guard-timer",
                idempotency_key=uuid4(),
                steps=[{"handler": "timer", "seconds": 0}],
            ),
        ),
        GuardCase(
            "apps.workflows.run_services.start_run",
            "tasks.reassign",
            lambda w: run_services.start_run(
                clinic_id=w.clinic,
                definition_version_id=w.definition.pk,
                context_refs=[{"kind": "clinic", "id": str(w.clinic)}],
                idempotency_key=uuid4(),
            ),
        ),
        GuardCase(
            "apps.workflows.task_services.add_comment",
            "tasks.view",
            lambda w: task_services.add_comment(
                clinic_id=w.clinic,
                task_id=w.assigned.pk,
                body="Sintetico guard",
                idempotency_key=uuid4(),
            ),
        ),
        GuardCase(
            "apps.workflows.task_services.assign_task",
            "tasks.assign",
            lambda w: task_services.assign_task(
                clinic_id=w.clinic,
                task_id=w.open_task.pk,
                owner=TaskOwner(user_id=w.actor),
                expected_revision=1,
            ),
        ),
        GuardCase(
            "apps.workflows.task_services.cancel_task",
            "tasks.reassign",
            lambda w: task_services.cancel_task(
                clinic_id=w.clinic, task_id=w.open_task.pk, expected_revision=1
            ),
        ),
        GuardCase(
            "apps.workflows.task_services.complete_task",
            "tasks.complete",
            lambda w: task_services.complete_task(
                clinic_id=w.clinic,
                task_id=w.review.pk,
                evidence={
                    "record": {"kind": "clinic", "id": str(w.clinic)},
                    "outcome": "reviewed",
                },
                expected_revision=3,
            ),
        ),
        GuardCase(
            "apps.workflows.task_services.create_task",
            "tasks.assign",
            lambda w: task_services.create_task(
                clinic_id=w.clinic, spec=w.creation(), idempotency_key=uuid4()
            ),
        ),
        GuardCase(
            "apps.workflows.task_services.escalate_task",
            "tasks.assign",
            lambda w: task_services.escalate_task(clinic_id=w.clinic, task_id=w.due.pk),
        ),
        GuardCase(
            "apps.workflows.task_services.list_tasks",
            "tasks.view",
            lambda w: task_services.list_tasks(clinic_id=w.clinic),
        ),
        GuardCase(
            "apps.workflows.task_services.start_task",
            "tasks.complete",
            lambda w: task_services.start_task(
                clinic_id=w.clinic, task_id=w.assigned.pk, expected_revision=2
            ),
        ),
        GuardCase(
            "apps.workflows.tasks.escalate",
            "tasks.assign",
            lambda w: tasks.escalate(task_id=str(w.due.pk)),
            owns_transaction=True,
            transport=True,
        ),
        GuardCase(
            "apps.workflows.tasks.execute_step",
            "tasks.reassign",
            lambda w: tasks.execute_step(step_id=str(w.pending.pk)),
            owns_transaction=True,
            transport=True,
        ),
        GuardCase(
            "apps.workflows.views._action",
            "tasks.complete",
            lambda w: views._action(
                w.post(
                    action="complete",
                    task_id=str(w.progress.pk),
                    expected_revision="3",
                    checked="on",
                ),
                w.clinic,
                w.actor,
            ),
        ),
        GuardCase(
            "apps.workflows.views._submit",
            "tasks.complete",
            lambda w: views._submit(
                w.post(
                    action="complete",
                    task_id=str(w.progress.pk),
                    expected_revision="3",
                    checked="on",
                ),
                Clinic.objects.get(pk=w.clinic),
                w.actor,
            ),
        ),
        GuardCase(
            "apps.workflows.views._authorized_tasks",
            "tasks.view",
            lambda w: inspect.unwrap(views._authorized_tasks)(w.request, w.clinic),
        ),
        GuardCase(
            "apps.workflows.views._bulk_action",
            "tasks.reassign",
            lambda w: views._bulk_action(
                w.post(action="bulk-preview", task_ids=str(w.open_task.pk), owner="me"),
                w.clinic,
                w.actor,
            ),
        ),
        GuardCase(
            "apps.workflows.views._manager",
            "tasks.reassign",
            lambda w: views._manager(w.clinic),
            predicate=True,
        ),
        GuardCase(
            "apps.workflows.views._staff_catalog",
            "tasks.view",
            lambda w: views._staff_catalog(w.clinic),
        ),
        GuardCase(
            "apps.workflows.views._task_row",
            "tasks.view",
            lambda w: views._task_row(w.assigned, {w.actor: "Sintetico"}),
        ),
        GuardCase(
            "apps.workflows.views.tasks",
            "tasks.view",
            lambda w: views.tasks(w.request, w.clinic),
        ),
    )


def allowed_result(value: object) -> bool:
    if isinstance(value, HttpResponseBase):
        assert value.status_code in {200, 403}
        return value.status_code == 200
    return not (value is False or value == "denied")
