"""Record scope: a referenced or target row's clinic against the call's clinic.

Every derived record-scope site, in Python lookups (``decision_sites``) and in
SQL functions and trigger statements (``sql_sites``), has a case here with a
same-clinic record (allowed) and an another-clinic record (refused). Refusals
by the services must equal the unknown-id denial and write nothing.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.comms.adapters import OperationScope
from apps.core import integration
from apps.core.integration import ActionOperationRequest, enqueue_operation
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.workflows import access, engine, external, views
from apps.workflows.models import Task, WorkflowRun, WorkflowStep
from apps.workflows.reassignment import apply_reassignment, preview_reassignment
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    assign_task,
    cancel_run,
    complete_task,
    create_task,
    list_tasks,
    publish_definition,
    start_run,
    start_task,
)
from django.db import DatabaseError, connection, transaction
from django.urls import resolve
from django.utils import timezone

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_cross_clinic_appointment_setups,
)
from workflows import decision_sites
from workflows.decision_sites import SCOPE, discover_sites
from workflows.sql_sites import discover_scope_conditions
from workflows.test_authority import _pending_writes

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
TIMER = [{"handler": "timer", "seconds": 0}]
KINDS = ("clinic", "enrollment", "appointment", "task")
REFUSALS = (CurrentActorError, WorkflowConflictError, WorkflowInputError)


@dataclass(frozen=True)
class Scoped:
    graph: RbacGraph
    staff: UUID
    manager: UUID
    references: dict[UUID, dict[str, dict[str, str]]]
    definitions: dict[UUID, UUID]

    @property
    def a(self) -> UUID:
        return self.graph.clinic_a

    @property
    def b(self) -> UUID:
        return self.graph.clinic_b

    def ref(self, clinic: UUID, kind: str) -> dict[str, str]:
        return self.references[clinic][kind]


def spec(clinic: UUID, subject: dict[str, str] | None = None) -> TaskSpec:
    return TaskSpec(
        kind="checklist",
        subject_ref=subject or {"kind": "clinic", "id": str(clinic)},
        due_at=timezone.now() + timedelta(hours=1),
    )


@pytest.fixture
def scoped(rbac_graph: RbacGraph) -> Scoped:
    graph = rbac_graph
    first, second = seed_cross_clinic_appointment_setups(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointments = {
            graph.clinic_a: create_synthetic_appointment(first).pk,
            # Same synthetic patient in both clinics: book a later slot.
            graph.clinic_b: create_synthetic_appointment(
                second, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
            ).pk,
        }
        own = {
            clinic: create_task(
                clinic_id=clinic, spec=spec(clinic), idempotency_key=uuid4()
            ).pk
            for clinic in (graph.clinic_a, graph.clinic_b)
        }
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=manager,
            role=UserClinicRole.Role.CLINIC_MANAGER,
        )
    with runtime_role(), tenant_context(manager, graph.organization_a):
        # Distinct keys: same-key publication across clinics is its own case.
        definitions = {
            clinic: publish_definition(
                clinic_id=clinic,
                key=f"scope-{index}",
                steps=TIMER,
                idempotency_key=uuid4(),
            ).pk
            for index, clinic in enumerate((graph.clinic_a, graph.clinic_b))
        }
    enrollments = {
        graph.clinic_a: first.enrollment_id,
        graph.clinic_b: second.enrollment_id,
    }
    references = {
        clinic: {
            "clinic": {"kind": "clinic", "id": str(clinic)},
            "enrollment": {"kind": "enrollment", "id": str(enrollments[clinic])},
            "appointment": {"kind": "appointment", "id": str(appointments[clinic])},
            "task": {"kind": "task", "id": str(own[clinic])},
        }
        for clinic in (graph.clinic_a, graph.clinic_b)
    }
    return Scoped(graph, graph.shared_user, manager, references, definitions)


def outcome(s: Scoped, actor: UUID, call: Callable[[], object]) -> tuple[object, bool]:
    """Run as the actor; return (result or refusal type+args, attempted writes)."""
    with permission_context(s.graph, actor):
        before = _pending_writes()
        try:
            result: object = call()
        except REFUSALS as error:
            result = (type(error), error.args)
        wrote = _pending_writes() != before
        transaction.set_rollback(True)
    return result, wrote


def same_denial(
    s: Scoped, actor: UUID, foreign: Callable[[], object], unknown: Callable[[], object]
) -> None:
    """Another clinic's record is refused like an unknown id and writes nothing."""
    refused, wrote = outcome(s, actor, foreign)
    baseline, baseline_wrote = outcome(s, actor, unknown)
    assert refused == baseline, (refused, baseline)
    assert isinstance(refused, tuple)
    assert issubclass(refused[0], CurrentActorError), refused
    assert not wrote
    assert not baseline_wrote


def unknown(kind: str) -> dict[str, str]:
    return {"kind": kind, "id": str(uuid4())}


# --- Python record-scope sites --------------------------------------------------


def reference(kind: str) -> Callable[[Scoped], None]:
    """require_reference, create_task, start_run and completion evidence."""

    def case(s: Scoped) -> None:
        foreign, mine = s.ref(s.b, kind), s.ref(s.a, kind)
        same_denial(
            s,
            s.staff,
            lambda: access.require_reference(clinic_id=s.a, value=foreign),
            lambda: access.require_reference(clinic_id=s.a, value=unknown(kind)),
        )
        assert (
            outcome(
                s, s.staff, lambda: access.require_reference(clinic_id=s.a, value=mine)
            )[0]
            == mine
        )
        same_denial(
            s,
            s.staff,
            lambda: create_task(
                clinic_id=s.a, spec=spec(s.a, foreign), idempotency_key=uuid4()
            ),
            lambda: create_task(
                clinic_id=s.a, spec=spec(s.a, unknown(kind)), idempotency_key=uuid4()
            ),
        )
        created, _wrote = outcome(
            s,
            s.staff,
            lambda: create_task(
                clinic_id=s.a, spec=spec(s.a, mine), idempotency_key=uuid4()
            ),
        )
        assert isinstance(created, Task)

        def run(refs: dict[str, str]) -> object:
            return start_run(
                clinic_id=s.a,
                definition_version_id=s.definitions[s.a],
                context_refs=[refs],
                idempotency_key=uuid4(),
            )

        same_denial(s, s.manager, lambda: run(foreign), lambda: run(unknown(kind)))
        assert isinstance(outcome(s, s.manager, lambda: run(mine))[0], WorkflowRun)

        def complete(record: dict[str, str]) -> object:
            task = create_task(
                clinic_id=s.a,
                spec=replace(spec(s.a), kind="review"),
                idempotency_key=uuid4(),
            )
            task = assign_task(
                clinic_id=s.a,
                task_id=task.pk,
                owner=TaskOwner(user_id=s.staff),
                expected_revision=1,
            )
            task = start_task(clinic_id=s.a, task_id=task.pk, expected_revision=2)
            return complete_task(
                clinic_id=s.a,
                task_id=task.pk,
                evidence={"record": record, "outcome": "reviewed"},
                expected_revision=3,
            )

        refused, _wrote = outcome(s, s.staff, lambda: complete(foreign))
        baseline, _wrote = outcome(s, s.staff, lambda: complete(unknown(kind)))
        assert refused == baseline
        assert isinstance(refused, tuple)
        assert refused[0] is access.WorkflowAccessDeniedError
        done = outcome(s, s.staff, lambda: complete(mine))[0]
        assert isinstance(done, Task)
        assert done.state == "done"

    return case


def task_scope(s: Scoped) -> None:
    foreign = UUID(s.ref(s.b, "task")["id"])
    same_denial(
        s,
        s.staff,
        lambda: access.require_task_access(
            clinic_id=s.a, task_id=foreign, permission="tasks.view"
        ),
        lambda: access.require_task_access(
            clinic_id=s.a, task_id=uuid4(), permission="tasks.view"
        ),
    )
    own = UUID(s.ref(s.a, "task")["id"])
    found = outcome(
        s,
        s.staff,
        lambda: access.require_task_access(
            clinic_id=s.a, task_id=own, permission="tasks.view"
        ),
    )[0]
    assert isinstance(found, Task)


def role_scope(s: Scoped) -> None:
    """A role held only in another clinic does not own this clinic's work."""
    member, _enrollment = permission_actor(s.graph, "receptionist")
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=member,
            role="nurse",
        )
    tasks: dict[UUID, Task] = {}
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        for clinic in (s.a, s.b):
            task = create_task(
                clinic_id=clinic, spec=spec(clinic), idempotency_key=uuid4()
            )
            tasks[clinic] = assign_task(
                clinic_id=clinic,
                task_id=task.pk,
                owner=TaskOwner(role="nurse"),
                expected_revision=1,
            )
    assert outcome(s, member, lambda: access.owner_matches(tasks[s.a]))[0] is False
    assert outcome(s, member, lambda: access.owner_matches(tasks[s.b]))[0] is True


def send_scope(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration, "_dispatch", lambda _operation: None)
    external.register_adapters()
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        definition = publish_definition(
            clinic_id=s.a,
            key="scope-send",
            steps=[{"handler": "external", "provider": "workflow-synthetic-v1"}],
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=s.a,
            definition_version_id=definition.pk,
            context_refs=[s.ref(s.a, "clinic")],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
    with runtime_role():
        assert engine.execute_step(step_id=step.pk) == "waiting"
    with owner_context(s.graph.organization_a):
        operation = WorkflowStep.objects.get(pk=step.pk).operation_id
    assert operation is not None

    def eligible(clinic: UUID) -> object:
        return external.step_send_eligible(
            OperationScope(
                operation_id=operation,
                organization_id=s.graph.organization_a,
                clinic_id=clinic,
                actor_id=s.manager,
            )
        )

    assert outcome(s, s.manager, lambda: eligible(s.b))[0] is False
    assert outcome(s, s.manager, lambda: eligible(s.a))[0] is True


def preview_scope(s: Scoped) -> None:
    foreign_task = UUID(s.ref(s.b, "task")["id"])
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        token = preview_reassignment(
            clinic_id=s.b, task_ids=[foreign_task], owner=TaskOwner(user_id=s.manager)
        ).token
    refused = outcome(
        s, s.manager, lambda: apply_reassignment(clinic_id=s.a, preview_token=token)
    )[0]
    assert isinstance(refused, tuple)
    assert refused[0] is WorkflowConflictError
    applied = outcome(
        s, s.manager, lambda: apply_reassignment(clinic_id=s.b, preview_token=token)
    )[0]
    assert isinstance(applied, tuple)
    assert isinstance(applied[0], Task)
    assert applied[0].owner_user_id == s.manager


def publication_scope(s: Scoped) -> None:
    key = uuid4()
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        publish_definition(
            clinic_id=s.b, key="shared", steps=TIMER, idempotency_key=key
        )
        publish_definition(
            clinic_id=s.b, key="shared", steps=TIMER, idempotency_key=uuid4()
        )
        mine = publish_definition(
            clinic_id=s.a, key="shared", steps=TIMER, idempotency_key=key
        )
    assert mine.clinic_id == s.a
    assert mine.version == 1


def run_scope(s: Scoped) -> None:
    def start(clinic: UUID, definition: UUID, key: UUID) -> object:
        return start_run(
            clinic_id=clinic,
            definition_version_id=definition,
            context_refs=[s.ref(clinic, "clinic")],
            idempotency_key=key,
        )

    same_denial(
        s,
        s.manager,
        lambda: start(s.a, s.definitions[s.b], uuid4()),
        lambda: start(s.a, uuid4(), uuid4()),
    )
    key = uuid4()
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        start(s.b, s.definitions[s.b], key)
        mine = start(s.a, s.definitions[s.a], key)
    assert isinstance(mine, WorkflowRun)
    assert mine.clinic_id == s.a


def cancel_scope(s: Scoped) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        runs = {
            clinic: start_run(
                clinic_id=clinic,
                definition_version_id=s.definitions[clinic],
                context_refs=[s.ref(clinic, "clinic")],
                idempotency_key=uuid4(),
            ).pk
            for clinic in (s.a, s.b)
        }
    same_denial(
        s,
        s.manager,
        lambda: cancel_run(clinic_id=s.a, run_id=runs[s.b]),
        lambda: cancel_run(clinic_id=s.a, run_id=uuid4()),
    )
    cancelled = outcome(
        s, s.manager, lambda: cancel_run(clinic_id=s.a, run_id=runs[s.a])
    )[0]
    assert isinstance(cancelled, WorkflowRun)
    assert cancelled.state == "cancelled"


def task_key_scope(s: Scoped) -> None:
    key = uuid4()
    with runtime_role(), tenant_context(s.staff, s.graph.organization_a):
        create_task(clinic_id=s.b, spec=spec(s.b), idempotency_key=key)
        mine = create_task(clinic_id=s.a, spec=spec(s.a), idempotency_key=key)
    assert mine.clinic_id == s.a


def queue_scope(s: Scoped) -> None:
    listed = outcome(s, s.staff, lambda: list_tasks(clinic_id=s.a))[0]
    assert isinstance(listed, tuple)
    ids = {task.pk for task in listed}
    assert UUID(s.ref(s.a, "task")["id"]) in ids
    assert UUID(s.ref(s.b, "task")["id"]) not in ids


def exceptions_scope(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        for clinic in (s.a, s.b):
            start_run(
                clinic_id=clinic,
                definition_version_id=s.definitions[clinic],
                context_refs=[s.ref(clinic, "clinic")],
                idempotency_key=uuid4(),
            )
    request = verified_request(s.manager, verified_at=STEP_UP_NOW)
    request.method = "GET"
    request.path = request.path_info = f"/clinics/{s.a}/tasks/exceptions/"
    request.resolver_match = resolve(request.path)
    request.META.update(SERVER_NAME="testserver", SERVER_PORT="80")
    later = timezone.now() + timedelta(hours=1)
    # Both pending runs are stale an hour later; the GET writes nothing.
    monkeypatch.setattr(timezone, "now", lambda: later)
    response = outcome(
        s,
        s.manager,
        lambda: inspect.unwrap(views._authorized_tasks)(request, s.a, exceptions=True),
    )[0]
    content = getattr(response, "content", b"")
    assert content.count(b"data-run-state") == 1, content.count(b"data-run-state")


PYTHON = "apps.workflows."
PREVIEW = (
    PYTHON + "reassignment.apply_reassignment:scope:payload['clinic'] != str(clinic_id)"
)
PY_CASES: dict[str, Callable[..., None]] = {
    PYTHON + "access.require_task_access:scope:clinic_id=clinic_id": task_scope,
    PYTHON + "access.owner_matches:scope:clinic_id=task.clinic_id": role_scope,
    PYTHON + "access.require_reference:scope:identifier == clinic_id": reference(
        "clinic"
    ),
    PYTHON + "access.require_reference:scope:clinic_id=clinic_id": reference(
        "enrollment"
    ),
    PYTHON + "access.require_reference:scope:clinic_id=clinic_id#2": reference(
        "appointment"
    ),
    PYTHON + "access.require_reference:scope:clinic_id=clinic_id#3": reference("task"),
    PYTHON + "external.step_send_eligible:scope:clinic_id=scope.clinic_id": send_scope,
    PREVIEW: preview_scope,
    PYTHON
    + "run_services.publish_definition:scope:clinic_id=clinic_id": publication_scope,
    PYTHON
    + "run_services.publish_definition:scope:clinic_id=clinic_id#2": publication_scope,
    PYTHON + "run_services.start_run:scope:clinic_id=clinic_id": run_scope,
    PYTHON + "run_services.start_run:scope:clinic_id=clinic_id#2": run_scope,
    PYTHON + "run_services.cancel_run:scope:clinic_id=clinic_id": cancel_scope,
    PYTHON + "task_services.create_task:scope:clinic_id=clinic_id": task_key_scope,
    PYTHON + "task_services.list_tasks:scope:clinic_id=clinic_id": queue_scope,
    PYTHON + "views._authorized_tasks:scope:clinic_id=clinic_id": exceptions_scope,
}


# --- SQL record-scope sites -----------------------------------------------------


def refused(expected: tuple[str, str], action: Callable[[], object]) -> None:
    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        action()
    assert getattr(caught.value.__cause__, "sqlstate", None) == expected[0]
    assert expected[1] in str(caught.value), str(caught.value)


def raw_task(
    s: Scoped,
    clinic: UUID,
    creator: UUID,
    subject: dict[str, str],
    key: UUID | None = None,
) -> Task:
    return Task.objects.create(
        organization_id=s.graph.organization_a,
        clinic_id=clinic,
        kind="checklist",
        subject_ref=subject,
        created_by_id=creator,
        due_at=timezone.now() + timedelta(hours=1),
        idempotency_key=key or uuid4(),
        fingerprint="0" * 64,
    )


def sql_reference(kind: str) -> Callable[[Scoped], None]:
    def case(s: Scoped) -> None:
        with permission_context(s.graph, s.staff):
            refused(
                ("23514", "invalid task"),
                lambda: raw_task(s, s.a, s.staff, s.ref(s.b, kind)),
            )
            raw_task(s, s.a, s.staff, s.ref(s.a, kind))

    return case


def sql_clinic_in_tenant(s: Scoped) -> None:
    with permission_context(s.graph, s.staff):
        refused(
            ("23514", "workflow scope mismatch"),
            lambda: raw_task(
                s,
                s.graph.clinic_c,
                s.staff,
                {"kind": "clinic", "id": str(s.graph.clinic_c)},
            ),
        )
        raw_task(s, s.a, s.staff, s.ref(s.a, "clinic"))


def sql_comment(s: Scoped) -> None:
    def comment(clinic: UUID, task: str) -> object:
        from apps.workflows.models import TaskComment  # noqa: PLC0415

        return TaskComment.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=clinic,
            task_id=UUID(task),
            author_id=s.manager,
            body="Sintetico",
        )

    with permission_context(s.graph, s.manager):
        refused(
            ("42501", "comment denied"), lambda: comment(s.a, s.ref(s.b, "task")["id"])
        )
        comment(s.a, s.ref(s.a, "task")["id"])


def sql_definition_version(s: Scoped) -> None:
    from apps.workflows.models import WorkflowDefinitionVersion  # noqa: PLC0415

    def definition(version: int) -> object:
        return WorkflowDefinitionVersion.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.a,
            key="scope-only-b",
            version=version,
            steps=TIMER,
            published_by_id=s.manager,
            idempotency_key=uuid4(),
            fingerprint="0" * 64,
        )

    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        publish_definition(
            clinic_id=s.b, key="scope-only-b", steps=TIMER, idempotency_key=uuid4()
        )
    with permission_context(s.graph, s.manager):
        refused(("23514", "definition denied"), lambda: definition(2))
        definition(1)


def _raw_run(s: Scoped, clinic: UUID, definition: UUID) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=s.graph.organization_a,
        clinic_id=clinic,
        definition_version_id=definition,
        started_by_id=s.manager,
        context_refs=[s.ref(clinic, "clinic")],
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def sql_run_definition(s: Scoped) -> None:
    with permission_context(s.graph, s.manager):
        refused(("23514", "run denied"), lambda: _raw_run(s, s.a, s.definitions[s.b]))
        _raw_run(s, s.a, s.definitions[s.a])


def sql_step_run(s: Scoped) -> None:
    with permission_context(s.graph, s.manager):
        runs = {
            clinic: _raw_run(s, clinic, s.definitions[clinic]) for clinic in (s.a, s.b)
        }

        def step(run: WorkflowRun) -> object:
            return WorkflowStep.objects.create(
                organization_id=s.graph.organization_a,
                clinic_id=s.a,
                run=run,
                position=0,
            )

        refused(("42501", "step authority denied"), lambda: step(runs[s.b]))
        step(runs[s.a])


def _claimed_step(s: Scoped) -> WorkflowStep:
    with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
        run = start_run(
            clinic_id=s.a,
            definition_version_id=s.definitions[s.a],
            context_refs=[s.ref(s.a, "clinic")],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
        assert engine.claim_step(step_id=step.pk) == 1
    return step


def sql_step_operation(s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration, "_dispatch", lambda _operation: None)
    external.register_adapters()
    step = _claimed_step(s)
    operations = {}
    for clinic in (s.a, s.b):
        with runtime_role(), tenant_context(s.manager, s.graph.organization_a):
            operations[clinic] = enqueue_operation(
                ActionOperationRequest(
                    provider="workflow-synthetic-v1",
                    clinic_id=clinic,
                    subject_type="workflows.step",
                    subject_id=step.pk,
                    idempotency_key=uuid4(),
                    payload_digest="0" * 64,
                )
            )

    def attach(operation: object) -> object:
        return WorkflowStep.objects.filter(pk=step.pk).update(
            state="waiting", operation_id=operation
        )

    with permission_context(s.graph, s.manager):
        refused(("23514", "step operation mismatch"), lambda: attach(operations[s.b]))
        assert attach(operations[s.a]) == 1


def sql_step_task(s: Scoped) -> None:
    step = _claimed_step(s)
    with permission_context(s.graph, s.manager):
        tasks = {
            clinic: raw_task(s, clinic, s.manager, s.ref(clinic, "clinic"), key=step.pk)
            for clinic in (s.a, s.b)
        }

        def attach(task: Task) -> object:
            return WorkflowStep.objects.filter(pk=step.pk).update(
                state="completed", created_task_id=task.pk
            )

        refused(("23514", "step task mismatch"), lambda: attach(tasks[s.b]))
        assert attach(tasks[s.a]) == 1


def _only_in_b(s: Scoped, role: str) -> UUID:
    user = User.objects.create(username=f"sintetico-only-b-{uuid4().hex}").pk
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=user,
            role=role,
        )
    return user


def _scalar(sql: str, arguments: list[UUID | str | None]) -> object:
    with connection.cursor() as cursor:
        cursor.execute(sql, arguments)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def sql_owned(s: Scoped) -> None:
    member, _enrollment = permission_actor(s.graph, "receptionist")
    with owner_context(s.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=s.graph.organization_a,
            clinic_id=s.b,
            user_id=member,
            role="nurse",
        )
    query = "SELECT clinic_app.workflows_owned(%s,NULL,'nurse')"
    with permission_context(s.graph, member):
        assert _scalar(query, [s.a]) is False
        assert _scalar(query, [s.b]) is True


def sql_owner_valid(s: Scoped) -> None:
    outsider = _only_in_b(s, "nurse")
    query = "SELECT clinic_app.workflows_owner_valid(%s,%s,'')"
    with permission_context(s.graph, s.manager):
        assert _scalar(query, [s.a, outsider]) is False
        assert _scalar(query, [s.b, outsider]) is True


def sql_staff_catalog(s: Scoped) -> None:
    outsider = _only_in_b(s, "nurse")
    query = (
        "SELECT count(*) FROM clinic_app.workflows_staff_catalog(%s) WHERE user_id=%s"
    )
    with permission_context(s.graph, s.manager):
        assert _scalar(query, [s.a, outsider]) == 0
        assert _scalar(query, [s.b, outsider]) == 1


GUARD = "scope:workflows_guard():"
REFERENCE = "scope:workflows_reference_valid(jsonb,uuid,uuid):"
SQL_CASES: dict[str, Callable[..., None]] = {
    GUARD + "c.id=NEW.clinic_id": sql_clinic_in_tenant,
    GUARD + "t.clinic_id=NEW.clinic_id": sql_comment,
    GUARD + "d.clinic_id=NEW.clinic_id": sql_definition_version,
    GUARD + "d.clinic_id=NEW.clinic_id#2": sql_run_definition,
    GUARD + "r.clinic_id=NEW.clinic_id": sql_step_run,
    GUARD + "o.clinic_id=NEW.clinic_id": sql_step_operation,
    GUARD + "t.clinic_id=NEW.clinic_id#2": sql_step_task,
    "scope:workflows_owned(uuid,uuid,text):r.clinic_id=clinic": sql_owned,
    "scope:workflows_owner_valid(uuid,uuid,text):r.clinic_id=clinic": sql_owner_valid,
    REFERENCE + "identifier=clinic": sql_reference("clinic"),
    REFERENCE + "e.clinic_id=clinic": sql_reference("enrollment"),
    REFERENCE + "a.clinic_id=clinic": sql_reference("appointment"),
    REFERENCE + "t.clinic_id=clinic": sql_reference("task"),
    "scope:workflows_staff_catalog(uuid):r.clinic_id=clinic": sql_staff_catalog,
}


def test_every_record_scope_site_has_a_two_clinic_case() -> None:
    """Fail closed in both layers: each derived scope site has its case."""
    python = {site.sid for site in discover_sites().values() if site.kind == SCOPE}
    assert python == set(PY_CASES)
    assert set(discover_scope_conditions()) == set(SQL_CASES)


def _run(case: Callable[..., None], s: Scoped, monkeypatch: pytest.MonkeyPatch) -> None:
    if "monkeypatch" in inspect.signature(case).parameters:
        case(s, monkeypatch)
    else:
        case(s)


# One run per distinct case; a case may prove several sites of one service.
UNIQUE = {
    case.__qualname__ + str(getattr(case, "__closure__", "")): site
    for site, case in sorted({**PY_CASES, **SQL_CASES}.items(), reverse=True)
}


@pytest.mark.parametrize("site", sorted(UNIQUE.values()))
def test_record_scope_refuses_another_clinic_and_allows_its_own(
    scoped: Scoped, site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run({**PY_CASES, **SQL_CASES}[site], scoped, monkeypatch)


@pytest.mark.parametrize(
    "body",
    [
        "def planted(clinic_id):\n    return Unknown.lookup(clinic_id=clinic_id)\n",
        "def planted(clinic_id):\n"
        "    return Task.objects.annotate(clinic_id=clinic_id)\n",
    ],
)
def test_python_scope_derivation_fails_closed_on_unclassified_uses(body: str) -> None:
    node = ast.parse(body).body[0]
    assert isinstance(node, ast.FunctionDef)
    function = decision_sites._Function(
        "planted", access, Path(str(access.__file__)), node, access.owner_matches
    )
    with pytest.raises(AssertionError, match="unclassified"):
        decision_sites._scope_sites("planted", function)


def test_sql_scope_derivation_fails_closed_on_unclassified_uses() -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE FUNCTION clinic_app.workflows_plant(clinic uuid) "
                "RETURNS boolean LANGUAGE sql AS $$ SELECT clinic IS NOT NULL $$"
            )
        with pytest.raises(AssertionError, match="unclassified record-scope use"):
            discover_scope_conditions()
        transaction.set_rollback(True)
    assert set(discover_scope_conditions()) == set(SQL_CASES)
