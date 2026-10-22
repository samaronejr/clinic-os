"""Real records and independent permission expectations for every workflow guard."""

from __future__ import annotations

import copy
import inspect
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from threading import Event
from typing import TYPE_CHECKING, Any, TypedDict
from unittest.mock import patch
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from apps.comms.adapters import OperationScope, PermanentSendError
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.identity.current_context import _UnauthorizedActorError
from apps.identity.models import Clinic, RoleGrant, User, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows import (
    access,
    definition_services,
    designer,
    engine,
    external,
    handlers,
    inbox,
    inbox_views,
    queue_services,
    reassignment,
    run_control,
    run_services,
    run_views,
    task_commands,
    task_feed,
    task_services,
    tasks,
    today,
    views,
)
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.inbox import InboxPage, InboxScope
from apps.workflows.models import (
    Task,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowStep,
)
from apps.workflows.validation import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
)
from django.db import connection, connections
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

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
    other: UUID
    draft: WorkflowDefinitionVersion
    validated: WorkflowDefinitionVersion
    failed_run: WorkflowRun
    other_failed_run: WorkflowRun
    alternate: WorkflowDefinitionVersion
    dependent: Task

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
        request = copy.copy(self.request)
        request.method = "POST"
        request.POST = RequestFactory().post(request.path, data=fields).POST
        return request


Outcome = tuple[bool, type[BaseException] | None]


@dataclass(frozen=True)
class Cell:
    """One varied relation input: who owns, started, created or issued a record."""

    name: str
    invoke: Callable[[GuardWorld, Any], object]
    expected: Outcome
    role: str | None = None
    setup: Callable[[GuardWorld], object] | None = None
    check: Callable[[object, Any], bool] | None = None


@dataclass(frozen=True)
class GuardCase:
    symbol: str
    permission: str
    invoke: Callable[[GuardWorld], object]
    owns_transaction: bool = False
    predicate: bool = False
    transport: bool = False
    check: Callable[[object, Any], bool] | None = None
    cells: tuple[Cell, ...] = ()
    refresh: Callable[[GuardWorld], GuardWorld] | None = None
    narrowed: Mapping[str, tuple[Outcome, Callable[[object, Any], bool] | None]] = (
        field(default_factory=dict)
    )
    # Relation sites whose value the entrypoint fixes by construction; each
    # needs a passing cell that varies the stored relation (see test_authority).
    bound: frozenset[str] = frozenset()


@contextmanager
def acting(user: UUID) -> Iterator[None]:
    """Switch the transaction-local actor inside an open permission context."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_user_id', true)")
        row = cursor.fetchone()
        assert row is not None
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [str(user)]
        )
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [row[0]]
            )


def narrow(world: GuardWorld, role: str, permission: str) -> None:
    """Remove one bundle permission inside the cell's rolled-back transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
    RoleGrant.objects.create(
        organization_id=world.graph.organization_a,
        clinic_id=world.clinic,
        role=role,
        permission=permission,
        bundle_version=2,
        valid_from=timezone.now() - timedelta(days=1),
    )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")


def owned_task(
    world: GuardWorld,
    owner: TaskOwner | None,
    *,
    started: bool = False,
    kind: str = "checklist",
    starter: UUID | None = None,
) -> Task:
    """Create a task as another staff member, optionally assigned and started."""
    with acting(world.other):
        task = task_services.create_task(
            clinic_id=world.clinic,
            spec=replace(world.creation(), kind=kind, depends_on=None),
            idempotency_key=uuid4(),
        )
    if owner is None:
        return task
    with acting(world.other):
        task = task_services.assign_task(
            clinic_id=world.clinic, task_id=task.pk, owner=owner, expected_revision=1
        )
    if started:
        with acting(owner.user_id or starter or world.actor):
            task = task_services.start_task(
                clinic_id=world.clinic, task_id=task.pk, expected_revision=2
            )
    return task


def other_run(world: GuardWorld, *, claim: bool = False) -> WorkflowStep:
    """A run pinned to the seeded timer definition, started by another manager."""
    with acting(world.other):
        run = run_services.start_run(
            clinic_id=world.clinic,
            definition_version_id=world.definition.pk,
            context_refs=[{"kind": "clinic", "id": str(world.clinic)}],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
        if claim:
            assert engine.claim_step(step_id=step.pk) == 1
    return step


def _task_actions(response: object, task: object) -> set[str]:
    """Return the lifecycle actions rendered for exactly one task row."""
    assert isinstance(response, HttpResponse)
    content = response.content.decode()
    marker = f'data-task="{getattr(task, "pk", task)}"'
    if marker not in content:
        return set()
    row = content.split(marker, 1)[1].split("</article>", 1)[0]
    return {
        action
        for action in ("assign", "start", "complete", "cancel")
        if f'name="action" value="{action}"' in row
    }


def _queue_owned(response: object, world: object) -> bool:
    """Owner-only lifecycle buttons appear on the actor's own seeded work."""
    assert isinstance(world, GuardWorld)
    return "start" in _task_actions(response, world.assigned) and "complete" in (
        _task_actions(response, world.progress)
    )


# Refused view calls remember their own request: the shared 403 page renders
# the actor's shell, so the oracle re-renders it for exactly that request.
_REFUSAL_REQUESTS: WeakKeyDictionary[HttpResponse, HttpRequest] = WeakKeyDictionary()


def _remember(request: HttpRequest, view: Callable[[HttpRequest], object]) -> object:
    response = view(request)
    if isinstance(response, HttpResponse):
        _REFUSAL_REQUESTS[response] = request
        if response.status_code >= 400:
            assert _denied_page(response, None)
    return response


def _denied_page(response: object, _prepared: object) -> bool:
    """The refusal is the shared request-rendered 403 page (todo 13 contract)."""
    if not isinstance(response, HttpResponse):
        return False
    request = _REFUSAL_REQUESTS.get(response)
    return (
        request is not None
        and response.status_code == 403
        and response.content == render(request, "403.html", status=403).content
    )


def _seed_lifecycle(
    graph: RbacGraph,
    definition: WorkflowDefinitionVersion,
    other: UUID,
    patch: pytest.MonkeyPatch,
) -> tuple[
    WorkflowDefinitionVersion,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowRun,
    WorkflowDefinitionVersion,
]:
    reference = {"kind": "clinic", "id": str(graph.clinic_a)}
    draft = definition_services.create_definition(
        clinic_id=graph.clinic_a,
        key="guard-draft",
        steps=[{"handler": "timer", "seconds": 0}],
        idempotency_key=uuid4(),
    )
    validated = definition_services.create_definition(
        clinic_id=graph.clinic_a,
        key="guard-validated",
        steps=[{"handler": "timer", "seconds": 0}],
        idempotency_key=uuid4(),
    )
    validated = definition_services.validate_definition(
        clinic_id=graph.clinic_a,
        definition_version_id=validated.pk,
        expected_revision=validated.revision,
        idempotency_key=uuid4(),
    )
    failed_run = run_services.start_run(
        clinic_id=graph.clinic_a,
        definition_version_id=definition.pk,
        context_refs=[reference],
        idempotency_key=uuid4(),
    )
    failed_step = WorkflowStep.objects.get(run=failed_run)
    assert engine.claim_step(step_id=failed_step.pk) == 1
    with patch.context() as broken:
        broken.setitem(handlers.HANDLERS, "timer", lambda _step, _now: "completed")
        assert engine._apply_or_fail(failed_step.pk, 1) == "failed"
    failed_run.refresh_from_db()
    with acting(other):
        alternate = run_services.publish_definition(
            clinic_id=graph.clinic_a,
            key="guard-concurrent",
            steps=[{"handler": "timer", "seconds": 0}],
            idempotency_key=uuid4(),
        )
        other_failed_run = run_services.start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[reference],
            idempotency_key=uuid4(),
        )
        other_failed_step = WorkflowStep.objects.get(run=other_failed_run)
        assert engine.claim_step(step_id=other_failed_step.pk) == 1
        with patch.context() as broken:
            broken.setitem(handlers.HANDLERS, "timer", lambda _step, _now: "completed")
            assert engine._apply_or_fail(other_failed_step.pk, 1) == "failed"
        other_failed_run.refresh_from_db()
    return draft, validated, failed_run, other_failed_run, alternate


def _guard_request(actor: UUID, clinic: UUID) -> HttpRequest:
    request = verified_request(actor, verified_at=STEP_UP_NOW)
    request.method = "GET"
    request.path = f"/clinics/{clinic}/tasks/"
    request.path_info = request.path
    request.resolver_match = resolve(request.path)
    request.META.update(SERVER_NAME="testserver", SERVER_PORT="80")
    return request


def seed_guard_world(graph: RbacGraph, patch: pytest.MonkeyPatch) -> GuardWorld:
    actor = User.objects.create(username=f"sintetico-workflow-guard-{uuid4().hex}")
    other = User.objects.create(username=f"sintetico-workflow-other-{uuid4().hex}")
    with owner_context(graph.organization_a):
        for user, role in (
            (actor, "clinic_manager"),
            (other, "clinic_manager"),
            (other, "receptionist"),
        ):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=user,
                role=role,
                workflow_bundle_version=2,
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
        dependent = task_services.create_task(
            clinic_id=graph.clinic_a,
            spec=replace(specification, depends_on=parent.pk),
            idempotency_key=uuid4(),
        )
        dependent = task_services.assign_task(
            clinic_id=graph.clinic_a,
            task_id=dependent.pk,
            owner=TaskOwner(user_id=actor.pk),
            expected_revision=1,
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
        draft, validated, failed_run, other_failed_run, alternate = _seed_lifecycle(
            graph, definition, other.pk, patch
        )
    request = _guard_request(actor.pk, graph.clinic_a)
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
        other.pk,
        draft,
        validated,
        failed_run,
        other_failed_run,
        alternate,
        dependent,
    )


def _base_cases() -> tuple[GuardCase, ...]:
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
            "apps.workflows.access.may_self_claim",
            "tasks.assign",
            lambda w: access.may_self_claim(w.open_task, TaskOwner(user_id=w.actor)),
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
            "apps.workflows.handlers._task",
            "tasks.reassign",
            lambda w: handlers._task(w.task_step, engine.utc_now()),
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
            "apps.workflows.queue_services.escalate_task",
            "tasks.assign",
            lambda w: queue_services.escalate_task(
                clinic_id=w.clinic, task_id=w.due.pk
            ),
        ),
        GuardCase(
            "apps.workflows.queue_services.list_tasks",
            "tasks.view",
            lambda w: queue_services.list_tasks(clinic_id=w.clinic),
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
            "apps.workflows.task_commands._action",
            "tasks.complete",
            lambda w: task_commands._action(
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
            "apps.workflows.task_commands._submit",
            "tasks.complete",
            lambda w: task_commands._submit(
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
            "apps.workflows.task_commands._assignment_owner",
            "tasks.assign",
            lambda w: task_commands._assignment_owner(
                w.post(action="assign", owner="me"), w.clinic, w.open_task.pk, w.actor
            ),
        ),
        GuardCase(
            "apps.workflows.views._authorized_tasks",
            "tasks.view",
            lambda w: inspect.unwrap(views._authorized_tasks)(w.request, w.clinic),
        ),
        GuardCase(
            "apps.workflows.task_commands._bulk_action",
            "tasks.reassign",
            lambda w: task_commands._bulk_action(
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
            lambda w: _remember(
                w.request, lambda request: views.tasks(request, w.clinic)
            ),
            transport=True,
        ),
    )


DENIED: Outcome = (False, WorkflowAccessDeniedError)
UNAUTHORIZED: Outcome = (False, _UnauthorizedActorError)
CONFLICT: Outcome = (False, WorkflowConflictError)
ALLOWED: Outcome = (True, None)
REFUSED: Outcome = (False, None)
MANAGER = "clinic_manager"
# Without tasks.view, FORCE RLS hides the record: the identical unknown denial.
INVISIBLE = {"tasks.view": (DENIED, None)}
STARTER = "apps.workflows.engine._locked_step:relation:run.started_by_id != actor"


def _other_user(w: GuardWorld) -> TaskOwner:
    return TaskOwner(user_id=w.other)


def _ownership(
    invoke: Callable[[GuardWorld, Any], object],
    *,
    denied: Outcome,
    started: bool = False,
    kind: str = "checklist",
    check: Callable[..., Callable[[object, Any], bool]] | None = None,
) -> tuple[Cell, ...]:
    """Owner/assignee input: other user, held role, unheld role; staff and manager."""

    def setup(
        owner: Callable[[GuardWorld], TaskOwner], *, by_other: bool = False
    ) -> Callable[[GuardWorld], Task]:
        return lambda w: owned_task(
            w,
            owner(w),
            started=started,
            kind=kind,
            starter=w.other if by_other else None,
        )

    held = check(owned=True) if check else None
    unheld = check(owned=False) if check else None
    return (
        *(
            Cell(
                f"owned-by-another-user-as-{role or 'staff'}",
                invoke,
                denied,
                role,
                setup(_other_user),
                unheld,
            )
            for role in (None, MANAGER)
        ),
        Cell(
            "owned-by-a-held-role",
            invoke,
            ALLOWED,
            "nurse",
            setup(lambda _w: TaskOwner(role="nurse")),
            held,
        ),
        # A manager sees every task, so the unheld-role decision is evaluated.
        Cell(
            "owned-by-an-unheld-role",
            invoke,
            denied,
            MANAGER,
            setup(lambda _w: TaskOwner(role="receptionist"), by_other=True),
            unheld,
        ),
    )


def _row_owned(*, owned: bool) -> Callable[[object, object], bool]:
    return lambda row, _p: isinstance(row, dict) and row["owned"] is owned


def _complete(w: GuardWorld, task: Task) -> object:
    return task_commands._action(
        w.post(
            action="complete",
            task_id=str(task.pk),
            expected_revision=str(task.revision),
            checked="on",
        ),
        w.clinic,
        w.actor,
    )


def _rendered(*, owned: bool) -> Callable[[object, object], bool]:
    def check(response: object, task: object) -> bool:
        actions = _task_actions(response, task)
        return ("start" in actions) is owned

    return check


def _starter(
    invoke: Callable[[GuardWorld, WorkflowStep], object], *, claim: bool
) -> tuple[Cell, ...]:
    return (
        Cell(
            "run-started-by-another-manager",
            invoke,
            DENIED,
            setup=lambda w: other_run(w, claim=claim),
        ),
    )


def _sender(
    invoke: Callable[[GuardWorld], object], denied: Outcome
) -> tuple[Cell, ...]:
    def as_other(w: GuardWorld, _prepared: object) -> object:
        with acting(w.other):
            return invoke(w)

    return (Cell("sent-by-another-manager", as_other, denied),)


def _preview(
    owner: Callable[[GuardWorld], TaskOwner],
    task: Callable[[GuardWorld], Task],
    *,
    issuer: Callable[[GuardWorld], UUID] | None = None,
) -> Callable[[GuardWorld], str]:
    def setup(w: GuardWorld) -> str:
        with acting(issuer(w) if issuer else w.actor):
            return reassignment.preview_reassignment(
                clinic_id=w.clinic, task_ids=[task(w).pk], owner=owner(w)
            ).token

    return setup


def _apply(w: GuardWorld, token: object) -> object:
    assert isinstance(token, str)
    return reassignment.apply_reassignment(clinic_id=w.clinic, preview_token=token)


def _comment_by_other(w: GuardWorld) -> UUID:
    key = uuid4()
    with acting(w.other):
        task_services.add_comment(
            clinic_id=w.clinic,
            task_id=w.assigned.pk,
            body="Sintetico guard",
            idempotency_key=key,
        )
    return key


def _created_by_other(w: GuardWorld) -> UUID:
    key = uuid4()
    with acting(w.other):
        task_services.create_task(
            clinic_id=w.clinic, spec=w.creation(), idempotency_key=key
        )
    return key


def _run_by_other(w: GuardWorld) -> UUID:
    key = uuid4()
    with acting(w.other):
        run_services.start_run(
            clinic_id=w.clinic,
            definition_version_id=w.definition.pk,
            context_refs=[{"kind": "clinic", "id": str(w.clinic)}],
            idempotency_key=key,
        )
    return key


def _concurrent_run_collision(world: GuardWorld, _prepared: object) -> object:
    """Commit a different pinned version after lookup, before the real INSERT."""
    key = uuid4()
    inserting = Event()
    peer_finished = Event()

    def peer() -> WorkflowRun:
        assert inserting.wait(15)
        try:
            with (
                runtime_role(),
                tenant_context(world.other, world.graph.organization_a),
            ):
                return run_services.start_run(
                    clinic_id=world.clinic,
                    definition_version_id=world.alternate.pk,
                    context_refs=[{"kind": "clinic", "id": str(world.clinic)}],
                    idempotency_key=key,
                )
        finally:
            peer_finished.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        competing = pool.submit(peer)

        def gate(
            execute: Callable[..., Any],
            sql: str,
            params: object,
            many: bool,
            context: object,
        ) -> object:
            if "INSERT INTO" in sql and "workflows_workflowrun" in sql:
                inserting.set()
                assert peer_finished.wait(15)
                committed = competing.result(timeout=15)
                assert committed.definition_version_id == world.alternate.pk
                assert committed.started_by_id == world.other
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(gate):
                return run_services.start_run(
                    clinic_id=world.clinic,
                    definition_version_id=world.definition.pk,
                    context_refs=[{"kind": "clinic", "id": str(world.clinic)}],
                    idempotency_key=key,
                )
        finally:
            inserting.set()
            competing.result(timeout=15)


def _task_by_other(w: GuardWorld) -> None:
    with acting(w.other):
        handlers._task(w.task_step, engine.utc_now())


def _committed_other_run(w: GuardWorld) -> WorkflowStep:
    return other_run(w)


def _fresh_step(w: GuardWorld) -> GuardWorld:
    with permission_context(w.graph, w.actor):
        run = run_services.start_run(
            clinic_id=w.clinic,
            definition_version_id=w.definition.pk,
            context_refs=[{"kind": "clinic", "id": str(w.clinic)}],
            idempotency_key=uuid4(),
        )
        return replace(w, pending=WorkflowStep.objects.get(run=run))


def _fresh_due(w: GuardWorld) -> GuardWorld:
    with permission_context(w.graph, w.actor):
        due = task_services.create_task(
            clinic_id=w.clinic,
            spec=replace(
                w.creation(), depends_on=None, due_at=timezone.now() - timedelta(days=1)
            ),
            idempotency_key=uuid4(),
        )
    return replace(w, due=due)


def _no_create(response: object, _prepared: object) -> bool:
    assert isinstance(response, HttpResponse)
    return response.status_code == 200 and b"task-create" not in response.content


def _no_lifecycle(response: object, _prepared: object) -> bool:
    assert isinstance(response, HttpResponse)
    return (
        response.status_code == 200
        and b'name="action" value="start"' not in response.content
        and b'name="action" value="complete"' not in response.content
    )


def _no_patients(response: object, prepared: object) -> bool:
    assert isinstance(response, HttpResponse)
    assert isinstance(prepared, GuardWorld)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.has_permission('demographics.read',%s,NULL)",
            [prepared.clinic],
        )
        assert cursor.fetchone() == (False,)
    patient_url = reverse("intake:patient-list", args=(prepared.clinic,))
    return (
        response.status_code == 200
        and f'href="{patient_url}"'.encode() not in response.content
    )


def _retry_controls(response: object, prepared: object) -> bool:
    assert isinstance(response, HttpResponse)
    assert isinstance(prepared, GuardWorld)
    content = response.content.decode()
    own = content.split(f'data-run="{prepared.failed_run.pk}"', 1)[1].split(
        "</article>", 1
    )[0]
    other = content.split(f'data-run="{prepared.other_failed_run.pk}"', 1)[1].split(
        "</article>", 1
    )[0]
    return (
        'name="action" value="retry"' in own
        and 'name="action" value="retry"' not in other
    )


class CaseRelations(TypedDict, total=False):
    """Per-guard relation inputs merged onto the derived guard cases."""

    check: Callable[[object, Any], bool]
    cells: tuple[Cell, ...]
    refresh: Callable[[GuardWorld], GuardWorld]
    narrowed: Mapping[str, tuple[Outcome, Callable[[object, Any], bool] | None]]
    bound: frozenset[str]


def _extras() -> dict[str, CaseRelations]:
    queue_narrowed = {
        "tasks.assign": (ALLOWED, _no_create),
        "tasks.complete": (ALLOWED, _no_lifecycle),
        "demographics.read": (ALLOWED, _no_patients),
    }
    return {
        "apps.workflows.access.owner_matches": {
            "cells": _ownership(
                lambda _w, task: access.owner_matches(task), denied=REFUSED
            )
        },
        "apps.workflows.access.require_task_access": {
            "cells": _ownership(
                lambda w, task: access.require_task_access(
                    clinic_id=w.clinic,
                    task_id=task.pk,
                    permission="tasks.view",
                    owned=True,
                ),
                denied=DENIED,
            )
        },
        "apps.workflows.task_services.start_task": {
            "narrowed": INVISIBLE,
            "cells": _ownership(
                lambda w, task: task_services.start_task(
                    clinic_id=w.clinic, task_id=task.pk, expected_revision=2
                ),
                denied=DENIED,
            ),
        },
        "apps.workflows.task_services.complete_task": {
            "narrowed": INVISIBLE,
            "cells": _ownership(
                lambda w, task: task_services.complete_task(
                    clinic_id=w.clinic,
                    task_id=task.pk,
                    evidence={
                        "record": {"kind": "clinic", "id": str(w.clinic)},
                        "outcome": "reviewed",
                    },
                    expected_revision=3,
                ),
                denied=DENIED,
                started=True,
                kind="review",
            ),
        },
        "apps.workflows.task_commands._action": {
            "narrowed": INVISIBLE,
            "cells": _ownership(_complete, denied=DENIED, started=True),
        },
        "apps.workflows.task_commands._submit": {
            "narrowed": INVISIBLE,
            "cells": (
                *_ownership(
                    lambda w, task: task_commands._submit(
                        w.post(
                            action="complete",
                            task_id=str(task.pk),
                            expected_revision=str(task.revision),
                            checked="on",
                        ),
                        Clinic.objects.get(pk=w.clinic),
                        w.actor,
                    ),
                    denied=DENIED,
                    started=True,
                ),
                Cell(
                    "create-refused-before-form-validation",
                    lambda w, _p: task_commands._submit(
                        w.post(action="create"),
                        Clinic.objects.get(pk=w.clinic),
                        w.actor,
                    ),
                    UNAUTHORIZED,
                    "nurse",
                    lambda w: narrow(w, "nurse", "tasks.assign"),
                ),
            ),
        },
        "apps.workflows.task_commands._assignment_owner": {
            "cells": (
                Cell(
                    "malformed-owner-is-validated-after-authority",
                    lambda w, _p: task_commands._assignment_owner(
                        w.post(action="assign", owner="user:not-a-uuid"),
                        w.clinic,
                        w.open_task.pk,
                        w.actor,
                    ),
                    (False, WorkflowInputError),
                ),
                Cell(
                    "malformed-owner-from-an-unpermitted-actor-is-denied",
                    lambda w, _p: task_commands._assignment_owner(
                        w.post(action="assign", owner="user:not-a-uuid"),
                        w.clinic,
                        w.open_task.pk,
                        w.actor,
                    ),
                    UNAUTHORIZED,
                    "nurse",
                    lambda w: narrow(w, "nurse", "tasks.assign"),
                ),
            )
        },
        "apps.workflows.views._task_row": {
            "check": lambda row, _p: isinstance(row, dict) and row["owned"] is True,
            "cells": _ownership(
                lambda _w, task: views._task_row(task, {}),
                denied=ALLOWED,
                check=_row_owned,
            ),
        },
        "apps.workflows.views._authorized_tasks": {
            "check": _queue_owned,
            "narrowed": queue_narrowed,
            "cells": (
                *_ownership(
                    lambda w, _task: inspect.unwrap(views._authorized_tasks)(
                        w.request, w.clinic
                    ),
                    denied=ALLOWED,
                    check=_rendered,
                ),
                Cell(
                    "complete-own-task",
                    lambda w, task: inspect.unwrap(views._authorized_tasks)(
                        w.post(
                            action="complete",
                            task_id=str(task.pk),
                            expected_revision="3",
                            checked="on",
                        ),
                        w.clinic,
                    ),
                    ALLOWED,
                    MANAGER,
                    lambda w: owned_task(w, TaskOwner(user_id=w.actor), started=True),
                ),
                Cell(
                    "complete-another-users-task",
                    lambda w, task: _remember(
                        w.post(
                            action="complete",
                            task_id=str(task.pk),
                            expected_revision="3",
                            checked="on",
                        ),
                        lambda request: inspect.unwrap(views._authorized_tasks)(
                            request, w.clinic
                        ),
                    ),
                    REFUSED,
                    MANAGER,
                    lambda w: owned_task(w, _other_user(w), started=True),
                    _denied_page,
                ),
            ),
        },
        "apps.workflows.views.tasks": {
            "check": _queue_owned,
            "narrowed": queue_narrowed,
            "cells": (
                *_ownership(
                    lambda w, _task: views.tasks(w.request, w.clinic),
                    denied=ALLOWED,
                    check=_rendered,
                ),
                Cell(
                    "complete-own-task",
                    lambda w, task: views.tasks(
                        w.post(
                            action="complete",
                            task_id=str(task.pk),
                            expected_revision="3",
                            checked="on",
                        ),
                        w.clinic,
                    ),
                    ALLOWED,
                    MANAGER,
                    lambda w: owned_task(w, TaskOwner(user_id=w.actor), started=True),
                ),
                Cell(
                    "complete-another-users-task",
                    lambda w, task: _remember(
                        w.post(
                            action="complete",
                            task_id=str(task.pk),
                            expected_revision="3",
                            checked="on",
                        ),
                        lambda request: views.tasks(request, w.clinic),
                    ),
                    REFUSED,
                    MANAGER,
                    lambda w: owned_task(w, _other_user(w), started=True),
                    _denied_page,
                ),
            ),
        },
        "apps.workflows.task_services.assign_task": {
            "cells": (
                Cell(
                    "claim-open-task-created-by-another",
                    lambda w, task: task_services.assign_task(
                        clinic_id=w.clinic,
                        task_id=task.pk,
                        owner=TaskOwner(user_id=w.actor),
                        expected_revision=1,
                    ),
                    # Invisible to non-managers: the identical unknown-record denial.
                    DENIED,
                    setup=lambda w: owned_task(w, None),
                ),
                Cell(
                    "give-own-open-task-to-another-user",
                    lambda w, _p: task_services.assign_task(
                        clinic_id=w.clinic,
                        task_id=w.open_task.pk,
                        owner=_other_user(w),
                        expected_revision=1,
                    ),
                    UNAUTHORIZED,
                ),
                Cell(
                    "reclaim-own-task-created-by-another",
                    lambda w, task: task_services.assign_task(
                        clinic_id=w.clinic,
                        task_id=task.pk,
                        owner=TaskOwner(user_id=w.actor),
                        expected_revision=2,
                    ),
                    ALLOWED,
                    setup=lambda w: owned_task(w, TaskOwner(user_id=w.actor)),
                ),
                Cell(
                    "manager-claims-open-task-created-by-another",
                    lambda w, task: task_services.assign_task(
                        clinic_id=w.clinic,
                        task_id=task.pk,
                        owner=TaskOwner(user_id=w.actor),
                        expected_revision=1,
                    ),
                    ALLOWED,
                    MANAGER,
                    lambda w: owned_task(w, None),
                ),
                Cell(
                    "manager-transfers-another-users-task",
                    lambda w, task: task_services.assign_task(
                        clinic_id=w.clinic,
                        task_id=task.pk,
                        owner=TaskOwner(user_id=w.actor),
                        expected_revision=2,
                    ),
                    ALLOWED,
                    MANAGER,
                    lambda w: owned_task(w, _other_user(w)),
                ),
            )
        },
        "apps.workflows.access.may_self_claim": {
            # Records are loaded with a manager's visibility: the relation, not
            # row-level security, must refuse staff who neither created nor own.
            "cells": (
                Cell(
                    "open-task-created-by-another",
                    lambda w, task: access.may_self_claim(
                        task, TaskOwner(user_id=w.actor)
                    ),
                    REFUSED,
                    setup=lambda w: owned_task(w, None),
                ),
                Cell(
                    "assigned-task-owned-by-another",
                    lambda w, task: access.may_self_claim(
                        task, TaskOwner(user_id=w.actor)
                    ),
                    REFUSED,
                    setup=lambda w: owned_task(w, _other_user(w)),
                ),
                Cell(
                    "own-open-task-to-another-user",
                    lambda w, _p: access.may_self_claim(w.open_task, _other_user(w)),
                    REFUSED,
                ),
                Cell(
                    "role-owner",
                    lambda w, _p: access.may_self_claim(
                        w.open_task, TaskOwner(role="nurse")
                    ),
                    REFUSED,
                ),
                Cell(
                    "owned-task-created-by-another",
                    lambda w, task: access.may_self_claim(
                        task, TaskOwner(user_id=w.actor)
                    ),
                    ALLOWED,
                    setup=lambda w: owned_task(w, TaskOwner(user_id=w.actor)),
                ),
            )
        },
        "apps.workflows.reassignment.apply_reassignment": {
            "cells": (
                Cell(
                    "preview-issued-by-another-manager",
                    _apply,
                    CONFLICT,
                    setup=_preview(
                        lambda w: TaskOwner(user_id=w.actor),
                        lambda w: w.open_task,
                        issuer=lambda w: w.other,
                    ),
                ),
                Cell(
                    "reassign-to-another-user",
                    _apply,
                    ALLOWED,
                    setup=_preview(_other_user, lambda w: w.open_task),
                ),
                Cell(
                    "claim-owned-task-created-by-another",
                    _apply,
                    ALLOWED,
                    setup=_preview(
                        lambda w: TaskOwner(user_id=w.actor),
                        lambda w: owned_task(w, TaskOwner(user_id=w.actor)),
                    ),
                ),
                Cell(
                    "claim-unowned-task-created-by-another",
                    _apply,
                    ALLOWED,
                    setup=_preview(
                        lambda w: TaskOwner(user_id=w.actor),
                        lambda w: owned_task(w, None),
                    ),
                ),
            )
        },
        "apps.workflows.run_services.start_run": {
            "narrowed": INVISIBLE,
            "cells": (
                Cell(
                    "replay-own-pinned-run-key",
                    lambda w, _p: run_services.start_run(
                        clinic_id=w.clinic,
                        definition_version_id=w.definition.pk,
                        context_refs=[{"kind": "clinic", "id": str(w.clinic)}],
                        idempotency_key=w.run.idempotency_key,
                    ),
                    ALLOWED,
                ),
                Cell(
                    "concurrent-key-bound-to-another-version",
                    _concurrent_run_collision,
                    CONFLICT,
                    MANAGER,
                ),
                Cell(
                    "replay-another-managers-run-key",
                    lambda w, key: run_services.start_run(
                        clinic_id=w.clinic,
                        definition_version_id=w.definition.pk,
                        context_refs=[{"kind": "clinic", "id": str(w.clinic)}],
                        idempotency_key=key,
                    ),
                    CONFLICT,
                    setup=_run_by_other,
                ),
            ),
        },
        "apps.workflows.task_services.add_comment": {
            "cells": (
                Cell(
                    "replay-another-authors-comment-key",
                    lambda w, key: task_services.add_comment(
                        clinic_id=w.clinic,
                        task_id=w.assigned.pk,
                        body="Sintetico guard",
                        idempotency_key=key,
                    ),
                    CONFLICT,
                    setup=_comment_by_other,
                ),
            )
        },
        "apps.workflows.task_services.create_task": {
            "cells": (
                Cell(
                    "replay-another-creators-key",
                    lambda w, key: task_services.create_task(
                        clinic_id=w.clinic, spec=w.creation(), idempotency_key=key
                    ),
                    CONFLICT,
                    setup=_created_by_other,
                ),
                Cell(
                    "manager-replays-another-creators-key",
                    lambda w, key: task_services.create_task(
                        clinic_id=w.clinic, spec=w.creation(), idempotency_key=key
                    ),
                    CONFLICT,
                    MANAGER,
                    _created_by_other,
                ),
            )
        },
        "apps.workflows.handlers._task": {
            "cells": (
                Cell(
                    "step-effect-created-by-another-manager",
                    lambda w, _p: handlers._task(w.task_step, engine.utc_now()),
                    CONFLICT,
                    setup=_task_by_other,
                ),
            )
        },
        "apps.workflows.engine._locked_step": {
            "cells": _starter(
                lambda _w, step: engine._locked_step(step.pk), claim=False
            )
        },
        "apps.workflows.engine.claim_step": {
            "cells": _starter(
                lambda _w, step: engine.claim_step(step_id=step.pk), claim=False
            )
        },
        "apps.workflows.engine.apply_claim": {
            "cells": _starter(
                lambda _w, step: engine.apply_claim(step_id=step.pk, fencing_token=1),
                claim=True,
            )
        },
        "apps.workflows.engine._apply_or_fail": {
            "cells": _starter(
                lambda _w, step: engine._apply_or_fail(step.pk, 1), claim=True
            )
        },
        "apps.workflows.engine.execute_step": {
            "refresh": _fresh_step,
            "bound": frozenset({STARTER}),
            "cells": (
                Cell(
                    "stored-starter-is-the-executing-actor",
                    lambda _w, step: engine.execute_step(step_id=step.pk),
                    ALLOWED,
                    setup=_committed_other_run,
                ),
            ),
        },
        "apps.workflows.tasks.execute_step": {
            "refresh": _fresh_step,
            "bound": frozenset({STARTER}),
            "cells": (
                Cell(
                    "stored-starter-is-the-executing-actor",
                    lambda _w, step: tasks.execute_step(step_id=str(step.pk)),
                    ALLOWED,
                    setup=_committed_other_run,
                ),
            ),
        },
        "apps.workflows.tasks.escalate": {"refresh": _fresh_due},
        "apps.workflows.external.step_send_eligible": {
            "cells": _sender(lambda w: external.step_send_eligible(w.scope), REFUSED)
        },
        "apps.workflows.external.SyntheticWorkflowAdapter.prepare": {
            "cells": _sender(
                lambda w: external.SyntheticWorkflowAdapter().prepare(w.operation),
                (False, PermanentSendError),
            )
        },
    }


def _retry_as_other(world: GuardWorld, _prepared: object) -> object:
    with acting(world.other):
        return run_control.retry_run(
            clinic_id=world.clinic,
            run_id=world.failed_run.pk,
            expected_revision=world.failed_run.revision,
            idempotency_key=uuid4(),
        )


def _native_retry_as_other(world: GuardWorld, _prepared: object) -> object:
    command: Callable[[HttpRequest, UUID], object] = run_views._command
    with acting(world.other):
        return command(
            world.post(
                action="retry",
                run_id=str(world.failed_run.pk),
                expected_revision=str(world.failed_run.revision),
                idempotency_key=str(uuid4()),
            ),
            world.clinic,
        )


def _lifecycle_cases() -> tuple[GuardCase, ...]:
    return (
        GuardCase(
            "apps.workflows.definition_services._definition",
            "tasks.reassign",
            lambda w: definition_services._definition(
                clinic_id=w.clinic, definition_version_id=w.definition.pk
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
        ),
        GuardCase(
            "apps.workflows.definition_services._command",
            "tasks.reassign",
            lambda w: definition_services._command(
                definition=copy.copy(w.draft),
                action="validate",
                expected_revision=w.draft.revision,
                idempotency_key=uuid4(),
                terms=w.draft.fingerprint,
            ),
        ),
        GuardCase(
            "apps.workflows.definition_services.create_definition",
            "tasks.reassign",
            lambda w: definition_services.create_definition(
                clinic_id=w.clinic,
                key="guard-new",
                steps=[{"handler": "timer", "seconds": 0}],
                idempotency_key=uuid4(),
            ),
        ),
        GuardCase(
            "apps.workflows.definition_services.revise_definition",
            "tasks.reassign",
            lambda w: definition_services.revise_definition(
                clinic_id=w.clinic,
                definition_version_id=w.draft.pk,
                steps=[{"handler": "timer", "seconds": 60}],
                expected_revision=w.draft.revision,
                idempotency_key=uuid4(),
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
        ),
        GuardCase(
            "apps.workflows.definition_services.validate_definition",
            "tasks.reassign",
            lambda w: definition_services.validate_definition(
                clinic_id=w.clinic,
                definition_version_id=w.draft.pk,
                expected_revision=w.draft.revision,
                idempotency_key=uuid4(),
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
        ),
        GuardCase(
            "apps.workflows.definition_services.publish_definition_draft",
            "tasks.reassign",
            lambda w: definition_services.publish_definition_draft(
                clinic_id=w.clinic,
                definition_version_id=w.validated.pk,
                expected_revision=w.validated.revision,
                idempotency_key=uuid4(),
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
        ),
        GuardCase(
            "apps.workflows.definition_services.retire_definition",
            "tasks.reassign",
            lambda w: definition_services.retire_definition(
                clinic_id=w.clinic,
                definition_version_id=w.definition.pk,
                expected_revision=w.definition.revision,
                idempotency_key=uuid4(),
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
        ),
        GuardCase(
            "apps.workflows.designer._command",
            "tasks.reassign",
            lambda w: designer._command(
                w.post(
                    action="create",
                    key="native-guard",
                    template="custom",
                    idempotency_key=str(uuid4()),
                ),
                w.clinic,
            ),
        ),
        GuardCase(
            "apps.workflows.designer._authorized_designer",
            "tasks.reassign",
            lambda w: inspect.unwrap(designer._authorized_designer)(
                w.request, w.clinic
            ),
            narrowed={"demographics.read": (ALLOWED, _no_patients)},
        ),
        GuardCase(
            "apps.workflows.designer.designer",
            "tasks.reassign",
            lambda w: _remember(
                w.request, lambda request: designer.designer(request, w.clinic)
            ),
            transport=True,
            narrowed={"demographics.read": (ALLOWED, _no_patients)},
        ),
        GuardCase(
            "apps.workflows.run_control.retry_run",
            "tasks.reassign",
            lambda w: run_control.retry_run(
                clinic_id=w.clinic,
                run_id=w.failed_run.pk,
                expected_revision=w.failed_run.revision,
                idempotency_key=uuid4(),
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
            cells=(
                Cell(
                    "retry-as-another-manager",
                    _retry_as_other,
                    (False, WorkflowAccessDeniedError),
                ),
            ),
        ),
        GuardCase(
            "apps.workflows.run_views._command",
            "tasks.reassign",
            lambda w: run_views._command(
                w.post(
                    action="retry",
                    run_id=str(w.failed_run.pk),
                    expected_revision=str(w.failed_run.revision),
                    idempotency_key=str(uuid4()),
                ),
                w.clinic,
            ),
            narrowed={"tasks.view": ((False, WorkflowAccessDeniedError), None)},
            cells=(
                Cell(
                    "native-retry-as-another-manager",
                    _native_retry_as_other,
                    (False, WorkflowAccessDeniedError),
                ),
            ),
        ),
        GuardCase(
            "apps.workflows.run_views._authorized_runs",
            "tasks.view",
            lambda w: inspect.unwrap(run_views._authorized_runs)(w.request, w.clinic),
            narrowed={"demographics.read": (ALLOWED, _no_patients)},
            cells=(
                Cell(
                    "retry-controls-bind-both-starters",
                    lambda w, _p: inspect.unwrap(run_views._authorized_runs)(
                        w.request, w.clinic
                    ),
                    ALLOWED,
                    MANAGER,
                    check=_retry_controls,
                ),
            ),
        ),
        GuardCase(
            "apps.workflows.run_views.runs",
            "tasks.view",
            lambda w: _remember(
                w.request, lambda request: run_views.runs(request, w.clinic)
            ),
            transport=True,
            narrowed={"demographics.read": (ALLOWED, _no_patients)},
            cells=(
                Cell(
                    "retry-controls-bind-both-starters",
                    lambda w, _p: run_views.runs(w.request, w.clinic),
                    ALLOWED,
                    MANAGER,
                    check=_retry_controls,
                ),
            ),
        ),
    )


def _owned_keys(world: GuardWorld) -> set[str]:
    return {
        f"task:{task.pk}"
        for task in (world.assigned, world.progress, world.review, world.dependent)
    }


def _inbox_owned(value: object, world: object) -> bool:
    """The actor's own active work is listed; finished work never is."""
    assert isinstance(world, GuardWorld)
    if isinstance(value, HttpResponse):
        content = value.content.decode()
        return all(f'data-inbox-key="{key}"' in content for key in _owned_keys(world))
    assert isinstance(value, InboxPage)
    keys = {item.key for item in value.items}
    return _owned_keys(world) <= keys and f"task:{world.parent.pk}" not in keys


def _inbox_without_tasks(value: object, _prepared: object) -> bool:
    if isinstance(value, HttpResponse):
        return 'data-inbox-kind="task"' not in value.content.decode()
    return isinstance(value, InboxPage) and all(
        item.kind != "task" for item in value.items
    )


def _feed_owned(value: object, world: object) -> bool:
    assert isinstance(world, GuardWorld)
    assert isinstance(value, tuple)
    return _owned_keys(world) <= {item.key for item in value}


def _today_without_counts(value: object, _prepared: object) -> bool:
    if isinstance(value, HttpResponse):
        return b'data-inbox-count="unavailable"' in value.content
    return getattr(value, "inbox", object()) is None


def _today_counts(value: object, _prepared: object) -> bool:
    if isinstance(value, HttpResponse):
        return b'data-inbox-count="task"' in value.content
    page = getattr(value, "inbox", None)
    return isinstance(page, InboxPage) and page.counts["task"] > 0


def _overflowed(value: object, _prepared: object) -> bool:
    if isinstance(value, HttpResponse):
        return b"+</dd>" in value.content
    page = value if isinstance(value, InboxPage) else getattr(value, "inbox", None)
    return isinstance(page, InboxPage) and page.truncated and "task" in page.overflowed


def _overflow_cell(invoke: Callable[[GuardWorld], object]) -> Cell:
    """Shrink the page below the world's owned tasks so the feed overflows."""

    def overflowing(world: GuardWorld, _prepared: object) -> object:
        with patch.object(inbox, "MAX_ITEMS", 1):
            return invoke(world)

    return Cell("feed-overflows-the-page", overflowing, ALLOWED, check=_overflowed)


# Entrypoints that assemble the queue decide whether a feed overflowed the page.
QUEUE_ASSEMBLERS = frozenset(
    {
        "apps.workflows.inbox.query_inbox",
        "apps.workflows.inbox_views._authorized_inbox",
        "apps.workflows.inbox_views.inbox",
        "apps.workflows.today.view_today",
        "apps.workflows.inbox_views._authorized_today",
        "apps.workflows.inbox_views.today",
    }
)


def _scope(world: GuardWorld) -> InboxScope:
    return InboxScope(
        clinic_id=world.clinic,
        actor_id=world.actor,
        timezone="America/Sao_Paulo",
        now=timezone.now(),
    )


# A narrowed task bundle hides only the task feed; the inbox gate is clinic
# demographics held through a clinical role, so the queue itself still opens.
INBOX_NARROWED = {"tasks.view": (ALLOWED, _inbox_without_tasks)}
TODAY_NARROWED = {
    "demographics.read": (ALLOWED, _today_without_counts),
    "tasks.view": (ALLOWED, None),
}


def _inbox_cases() -> tuple[GuardCase, ...]:
    return tuple(
        replace(case, cells=(_overflow_cell(case.invoke),))
        if case.symbol in QUEUE_ASSEMBLERS
        else case
        for case in _inbox_entrypoints()
    )


def _inbox_entrypoints() -> tuple[GuardCase, ...]:
    return (
        GuardCase(
            "apps.workflows.inbox.require_inbox",
            "demographics.read",
            lambda w: inbox.require_inbox(clinic_id=w.clinic),
        ),
        GuardCase(
            "apps.workflows.inbox.query_inbox",
            "demographics.read",
            lambda w: inbox.query_inbox(clinic_id=w.clinic),
            check=_inbox_owned,
            narrowed=INBOX_NARROWED,
        ),
        GuardCase(
            "apps.workflows.task_feed.task_items",
            "tasks.view",
            lambda w: task_feed.task_items(_scope(w)),
            check=_feed_owned,
        ),
        GuardCase(
            "apps.workflows.inbox_views._authorized_inbox",
            "demographics.read",
            lambda w: inspect.unwrap(inbox_views._authorized_inbox)(
                w.request, w.clinic
            ),
            check=_inbox_owned,
            narrowed=INBOX_NARROWED,
        ),
        GuardCase(
            "apps.workflows.inbox_views.inbox",
            "demographics.read",
            lambda w: _remember(
                w.request, lambda request: inbox_views.inbox(request, w.clinic)
            ),
            transport=True,
            check=_inbox_owned,
            narrowed=INBOX_NARROWED,
        ),
        GuardCase(
            "apps.workflows.today.view_today",
            "appointment.read_own",
            lambda w: today.view_today(clinic_id=w.clinic),
            check=_today_counts,
            narrowed=TODAY_NARROWED,
        ),
        GuardCase(
            "apps.workflows.inbox_views._authorized_today",
            "appointment.read_own",
            lambda w: inspect.unwrap(inbox_views._authorized_today)(
                w.request, w.clinic
            ),
            check=_today_counts,
            narrowed=TODAY_NARROWED,
        ),
        GuardCase(
            "apps.workflows.inbox_views.today",
            "appointment.read_own",
            lambda w: _remember(
                w.request, lambda request: inbox_views.today(request, w.clinic)
            ),
            transport=True,
            check=_today_counts,
            narrowed=TODAY_NARROWED,
        ),
    )


def guard_cases() -> tuple[GuardCase, ...]:
    extras = _extras()
    cases = tuple(
        replace(case, **extras.get(case.symbol, {}))
        for case in (*_base_cases(), *_lifecycle_cases(), *_inbox_cases())
    )
    assert set(extras) <= {case.symbol for case in cases}, set(extras)
    return cases


def allowed_result(value: object) -> bool:
    if isinstance(value, HttpResponseBase):
        assert value.status_code in {200, 403}
        return value.status_code == 200
    return not (value is False or value == "denied")
