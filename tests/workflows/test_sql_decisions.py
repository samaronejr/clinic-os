"""Every derived SQL decision refuses and allows through real RLS as clinic_app.

``sql_sites.discover_sql_sites`` derives the functions, policies and trigger
branches that read the actor. Each has one behavioural case here that makes
it decide both ways. Direct row writes bypass the Python services on purpose:
these decisions must hold on their own. The SQL mutant sweep forces each site
to allow and to deny and requires one of these tests to fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import RoleGrant, User
from apps.identity.permissions import BUNDLES_V2
from apps.tenancy.db import tenant_context
from apps.workflows.access import WorkflowAccessDeniedError, require_task_access
from apps.workflows.models import (
    Task,
    TaskComment,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowStep,
)
from apps.workflows.services import (
    TaskOwner,
    add_comment,
    assign_task,
    cancel_run,
    create_task,
    list_tasks,
    publish_definition,
    start_run,
)
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role
from workflows.sql_sites import discover_conditions, discover_sql_sites
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
RLS = ("42501", "row-level security")
NON_MANAGERS = sorted(
    role
    for role, permissions in BUNDLES_V2.items()
    if "tasks.view" in permissions and "tasks.reassign" not in permissions
)
TIMER = [{"handler": "timer", "seconds": 0}]


@dataclass(frozen=True)
class World:
    graph: RbacGraph
    manager: UUID
    other_manager: UUID
    nurse: UUID
    receptionist: UUID
    role_task: Task
    user_task: Task
    definition: WorkflowDefinitionVersion
    run: WorkflowRun
    step: WorkflowStep

    def acting(self, user: UUID) -> AbstractContextManager[None]:
        return permission_context(self.graph, user)

    @property
    def clinic(self) -> UUID:
        return self.graph.clinic_a

    @property
    def reference(self) -> dict[str, str]:
        return {"kind": "clinic", "id": str(self.clinic)}


@pytest.fixture
def world(rbac_graph: RbacGraph) -> World:
    graph = rbac_graph
    manager, _enrollment = permission_actor(graph, "clinic_manager")
    other_manager, _enrollment = permission_actor(graph, "clinic_manager")
    nurse, _enrollment = permission_actor(graph, "nurse")
    receptionist, _enrollment = permission_actor(graph, "receptionist")
    with runtime_role(), tenant_context(manager, graph.organization_a):
        role_task, user_task = (
            assign_task(
                clinic_id=graph.clinic_a,
                task_id=create_task(
                    clinic_id=graph.clinic_a, spec=spec(graph), idempotency_key=uuid4()
                ).pk,
                owner=owner,
                expected_revision=1,
            )
            for owner in (TaskOwner(role="nurse"), TaskOwner(user_id=nurse))
        )
        add_comment(
            clinic_id=graph.clinic_a,
            task_id=role_task.pk,
            body="Sintetico nota",
            idempotency_key=uuid4(),
        )
        definition = publish_definition(
            clinic_id=graph.clinic_a,
            key="sql-case",
            steps=TIMER,
            idempotency_key=uuid4(),
        )
        run = start_run(
            clinic_id=graph.clinic_a,
            definition_version_id=definition.pk,
            context_refs=[{"kind": "clinic", "id": str(graph.clinic_a)}],
            idempotency_key=uuid4(),
        )
        step = WorkflowStep.objects.get(run=run)
    return World(
        graph,
        manager,
        other_manager,
        nurse,
        receptionist,
        role_task,
        user_task,
        definition,
        run,
        step,
    )


def refused(expected: tuple[str, str], action: Callable[[], object]) -> None:
    """The exact SQL refusal: SQLSTATE and the deciding layer's message."""
    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        action()
    assert getattr(caught.value.__cause__, "sqlstate", None) == expected[0]
    assert expected[1] in str(caught.value), str(caught.value)


def narrow(world: World, role: str, permission: str) -> None:
    with owner_context(world.graph.organization_a):
        RoleGrant.objects.create(
            organization_id=world.graph.organization_a,
            clinic_id=world.clinic,
            role=role,
            permission=permission,
            bundle_version=2,
            valid_from=timezone.now() - timedelta(days=1),
        )


def raw_task(
    world: World, creator: UUID, *, due: timedelta = timedelta(hours=1)
) -> Task:
    return Task.objects.create(
        organization_id=world.graph.organization_a,
        clinic_id=world.clinic,
        kind="checklist",
        subject_ref=world.reference,
        created_by_id=creator,
        due_at=timezone.now() + due,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def command(task: UUID, **fields: object) -> int:
    """One revisioned task write, exactly as a service would issue it."""
    return Task.objects.filter(pk=task).update(
        revision=F("revision") + 1,
        last_command_key=uuid4(),
        last_command_digest="a" * 64,
        **fields,
    )


def unscoped(statement: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return int(cursor.rowcount)


def raw_comment(world: World, task: UUID, author: UUID) -> TaskComment:
    return TaskComment.objects.create(
        organization_id=world.graph.organization_a,
        clinic_id=world.clinic,
        task_id=task,
        author_id=author,
        body="Sintetico nota",
    )


def raw_definition(world: World, publisher: UUID) -> WorkflowDefinitionVersion:
    return WorkflowDefinitionVersion.objects.create(
        organization_id=world.graph.organization_a,
        clinic_id=world.clinic,
        key=f"sql-{uuid4().hex[:12]}",
        version=1,
        steps=TIMER,
        published_by_id=publisher,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def raw_run(world: World, starter: UUID) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=world.graph.organization_a,
        clinic_id=world.clinic,
        definition_version=world.definition,
        started_by_id=starter,
        context_refs=[world.reference],
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def visible(
    model: type[
        Task | TaskComment | WorkflowRun | WorkflowStep | WorkflowDefinitionVersion
    ],
) -> set[UUID]:
    return set(model.objects.values_list("pk", flat=True))


def scalar(sql: str, arguments: list[UUID | str | None]) -> object:
    with connection.cursor() as cursor:
        cursor.execute(sql, arguments)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


# --- Policies -----------------------------------------------------------------


def task_read(w: World) -> None:
    with w.acting(w.receptionist):
        assert visible(Task) == set()
    with w.acting(w.nurse):
        assert visible(Task) == {w.role_task.pk, w.user_task.pk}


def task_update(w: World) -> None:
    bump = (
        "UPDATE clinic_app.workflows_task SET revision=revision+1, "
        "last_command_key=gen_random_uuid(), last_command_digest=repeat('a',64)"
    )
    with w.acting(w.receptionist):
        assert unscoped(bump) == 0
        # A constant SET reads no old value, so only the UPDATE policy decides.
        assert unscoped("UPDATE clinic_app.workflows_task SET updated_at=now()") == 0
    with w.acting(w.nurse):
        assert unscoped(bump) == 2


def comment_read(w: World) -> None:
    with w.acting(w.receptionist):
        assert visible(TaskComment) == set()
    with w.acting(w.nurse):
        assert len(visible(TaskComment)) == 1


def definition_read(w: World) -> None:
    with w.acting(w.receptionist):
        assert visible(WorkflowDefinitionVersion) == {w.definition.pk}
    narrow(w, "receptionist", "tasks.view")
    with w.acting(w.receptionist):
        assert visible(WorkflowDefinitionVersion) == set()


def run_read(w: World) -> None:
    with w.acting(w.receptionist):
        assert visible(WorkflowRun) == set()
    with w.acting(w.other_manager):
        assert visible(WorkflowRun) == {w.run.pk}


def run_update(w: World) -> None:
    with w.acting(w.receptionist):
        assert unscoped("UPDATE clinic_app.workflows_workflowrun SET state=state") == 0
        assert (
            unscoped("UPDATE clinic_app.workflows_workflowrun SET state='running'") == 0
        )
    with runtime_role(), tenant_context(w.manager, w.graph.organization_a):
        assert cancel_run(clinic_id=w.clinic, run_id=w.run.pk).state == "cancelled"


def step_read(w: World) -> None:
    with w.acting(w.receptionist):
        assert visible(WorkflowStep) == set()
    with w.acting(w.other_manager):
        assert visible(WorkflowStep) == {w.step.pk}


def step_update(w: World) -> None:
    with w.acting(w.receptionist):
        assert unscoped("UPDATE clinic_app.workflows_workflowstep SET state=state") == 0
        assert (
            unscoped("UPDATE clinic_app.workflows_workflowstep SET state='running'")
            == 0
        )
    step_authority(w)


def owned(w: World) -> None:
    query = "SELECT clinic_app.workflows_owned(%s,%s,%s)"
    for user, expected in ((w.receptionist, False), (w.nurse, True)):
        with w.acting(user):
            assert scalar(query, [w.clinic, None, "nurse"]) is expected
            assert scalar(query, [w.clinic, w.nurse, ""]) is expected
    task_read(w)


def owner_valid(w: World) -> None:
    outsider = User.objects.create(username=f"sintetico-outsider-{uuid4().hex}").pk
    query = "SELECT clinic_app.workflows_owner_valid(%s,%s,%s)"
    with w.acting(w.manager):
        assert scalar(query, [w.clinic, w.nurse, ""]) is True
        assert scalar(query, [w.clinic, None, "nurse"]) is True
        assert scalar(query, [w.clinic, outsider, ""]) is False
        assert scalar(query, [w.clinic, None, "janitor"]) is False
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        assert scalar(query, [w.clinic, w.nurse, ""]) is False


def staff_catalog(w: World) -> None:
    query = "SELECT count(*) FROM clinic_app.workflows_staff_catalog(%s)"
    with w.acting(w.nurse):
        assert int(str(scalar(query, [w.clinic]))) >= 4
    narrow(w, "nurse", "tasks.view")
    with w.acting(w.nurse):
        assert scalar(query, [w.clinic]) == 0


# --- Trigger branches ---------------------------------------------------------


def invalid_task(w: World) -> None:
    with w.acting(w.nurse):
        raw_task(w, w.nurse)
        refused(("23514", "invalid task"), lambda: raw_task(w, w.receptionist))


def assignment(w: World) -> None:
    with w.acting(w.nurse):
        mine = raw_task(w, w.nurse)
        other = raw_task(w, w.nurse)
        refused(
            ("42501", "task assignment denied"),
            lambda: command(other.pk, owner_user_id=w.receptionist, state="assigned"),
        )
        assert command(mine.pk, owner_user_id=w.nurse, state="assigned") == 1


def completion(w: World) -> None:
    with w.acting(w.other_manager):
        refused(
            ("42501", "task completion denied"),
            lambda: command(w.user_task.pk, state="in_progress"),
        )
    with w.acting(w.nurse):
        assert command(w.user_task.pk, state="in_progress") == 1


def cancellation(w: World) -> None:
    with w.acting(w.nurse):
        refused(
            ("42501", "task cancellation denied"),
            lambda: command(w.user_task.pk, state="cancelled"),
        )
    with w.acting(w.manager):
        assert command(w.user_task.pk, state="cancelled") == 1


def escalation(w: World) -> None:
    with w.acting(w.nurse):
        early = raw_task(w, w.nurse)
        due = raw_task(w, w.nurse, due=-timedelta(hours=1))
        refused(
            ("23514", "invalid escalation"),
            lambda: command(early.pk, escalated_at=timezone.now()),
        )
        assert command(due.pk, escalated_at=timezone.now()) == 1


def comment_trigger(w: World) -> None:
    with w.acting(w.receptionist):
        refused(
            ("42501", "comment denied"),
            lambda: raw_comment(w, w.role_task.pk, w.receptionist),
        )
    with w.acting(w.nurse):
        refused(
            ("42501", "comment denied"),
            lambda: raw_comment(w, w.role_task.pk, w.receptionist),
        )
        raw_comment(w, w.role_task.pk, w.nurse)


def definition_trigger(w: World) -> None:
    with w.acting(w.manager):
        refused(
            ("23514", "definition denied"), lambda: raw_definition(w, w.other_manager)
        )
        raw_definition(w, w.manager)


def run_trigger(w: World) -> None:
    with w.acting(w.manager):
        refused(("23514", "run denied"), lambda: raw_run(w, w.other_manager))
        raw_run(w, w.manager)


def run_transition(w: World) -> None:
    with w.acting(w.manager):
        assert WorkflowRun.objects.filter(pk=w.run.pk).update(state="running") == 1
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        refused(
            ("42501", "run transition denied"),
            lambda: WorkflowRun.objects.filter(pk=w.run.pk).update(state="waiting"),
        )


def step_authority(w: World) -> None:
    def claim() -> int:
        return WorkflowStep.objects.filter(pk=w.step.pk).update(
            state="running",
            fencing_token=F("fencing_token") + 1,
            claim_until=timezone.now() + timedelta(minutes=2),
        )

    with w.acting(w.other_manager):
        refused(("42501", "step authority denied"), claim)
    with w.acting(w.manager):
        assert claim() == 1


GUARD = "branch:workflows_guard():"
CASES: dict[str, Callable[[World], None]] = {
    "policy:workflows_task.workflow_read": task_read,
    "policy:workflows_task.workflow_update": task_update,
    "policy:workflows_taskcomment.workflow_read": comment_read,
    "policy:workflows_workflowdefinitionversion.workflow_read": definition_read,
    "policy:workflows_workflowrun.workflow_read": run_read,
    "policy:workflows_workflowrun.workflow_update": run_update,
    "policy:workflows_workflowstep.workflow_read": step_read,
    "policy:workflows_workflowstep.workflow_update": step_update,
    "function:workflows_owned(uuid,uuid,text)": owned,
    "function:workflows_owner_valid(uuid,uuid,text)": owner_valid,
    "function:workflows_staff_catalog(uuid)": staff_catalog,
    GUARD + "invalid task": invalid_task,
    GUARD + "task assignment denied": assignment,
    GUARD + "task completion denied": completion,
    GUARD + "task cancellation denied": cancellation,
    GUARD + "invalid escalation": escalation,
    GUARD + "comment denied": comment_trigger,
    GUARD + "definition denied": definition_trigger,
    GUARD + "run denied": run_trigger,
    GUARD + "run transition denied": run_transition,
    GUARD + "step authority denied": step_authority,
}


def test_every_derived_sql_decision_has_a_behavioural_case() -> None:
    """Fail closed: a new actor-reading SQL decision needs a case here."""
    assert set(discover_sql_sites()) == set(CASES)


@pytest.mark.parametrize("site", sorted(CASES))
def test_sql_decision_refuses_and_allows(world: World, site: str) -> None:
    CASES[site](world)


# --- One condition at a time ----------------------------------------------------
# Each derived trigger condition gets an otherwise valid direct write in which
# exactly that input is missing: the permission narrowed from the actor's
# bundle, the helper's relation unmet, or the actor comparison forged.


def _refuses(message: str) -> tuple[str, str]:
    return (
        "23514"
        if message.startswith(("invalid", "definition", "run denied"))
        else "42501",
        message,
    )


def _bare_run(w: World) -> WorkflowRun:
    """A run the manager started directly: no steps yet, so position 0 is free."""
    with w.acting(w.manager):
        return raw_run(w, w.manager)


def _raw_step(
    w: World, run: WorkflowRun, state: str = "pending"
) -> Callable[[], object]:
    return lambda: WorkflowStep.objects.create(
        organization_id=w.graph.organization_a,
        clinic_id=w.clinic,
        run=run,
        position=0,
        state=state,
    )


def task_without_assign(w: World) -> None:
    narrow(w, "nurse", "tasks.assign")
    with w.acting(w.nurse):
        refused(_refuses("invalid task"), lambda: raw_task(w, w.nurse))


def assignment_to_an_invalid_owner(w: World) -> None:
    outsider = User.objects.create(username=f"sintetico-outsider-{uuid4().hex}").pk
    with w.acting(w.manager):
        refused(
            _refuses("task assignment denied"),
            lambda: command(w.user_task.pk, owner_user_id=outsider),
        )


def assignment_to_a_role_without_reassign(w: World) -> None:
    with w.acting(w.nurse):
        mine = raw_task(w, w.nurse)
        refused(
            _refuses("task assignment denied"),
            lambda: command(mine.pk, owner_role="nurse", state="assigned"),
        )


def start_without_complete(w: World) -> None:
    narrow(w, "nurse", "tasks.complete")
    with w.acting(w.nurse):
        refused(
            _refuses("task completion denied"),
            lambda: command(w.user_task.pk, state="in_progress"),
        )


def escalate_without_assign(w: World) -> None:
    with w.acting(w.nurse):
        due = raw_task(w, w.nurse, due=-timedelta(hours=1))
    narrow(w, "nurse", "tasks.assign")
    with w.acting(w.nurse):
        refused(
            _refuses("invalid escalation"),
            lambda: command(due.pk, escalated_at=timezone.now()),
        )


def comment_without_view(w: World) -> None:
    with w.acting(w.nurse):
        mine = raw_task(w, w.nurse)
    narrow(w, "nurse", "tasks.view")
    with w.acting(w.nurse):
        refused(_refuses("comment denied"), lambda: raw_comment(w, mine.pk, w.nurse))


def comment_on_anothers_open_task(w: World) -> None:
    with w.acting(w.manager):
        theirs = raw_task(w, w.manager)
    with w.acting(w.receptionist):
        refused(
            _refuses("comment denied"),
            lambda: raw_comment(w, theirs.pk, w.receptionist),
        )


def _manager_without(
    permission: str, write: Callable[[World, UUID], object], message: str
) -> Callable[[World], None]:
    def case(w: World) -> None:
        narrow(w, "clinic_manager", permission)
        with w.acting(w.manager):
            refused(_refuses(message), lambda: write(w, w.manager))

    return case


def transition_without_reassign(w: World) -> None:
    narrow(w, "clinic_manager", "tasks.reassign")
    with w.acting(w.manager):
        refused(
            _refuses("run transition denied"),
            lambda: WorkflowRun.objects.filter(pk=w.run.pk).update(state="running"),
        )


def step_by_another_manager(w: World) -> None:
    run = _bare_run(w)
    with w.acting(w.other_manager):
        refused(_refuses("step authority denied"), _raw_step(w, run))


def _starter_step_without(permission: str) -> Callable[[World], None]:
    def case(w: World) -> None:
        run = _bare_run(w)
        narrow(w, "clinic_manager", permission)
        with w.acting(w.manager):
            refused(_refuses("step authority denied"), _raw_step(w, run))

    return case


def _cancel_step_without(permission: str) -> Callable[[World], None]:
    def case(w: World) -> None:
        run = _bare_run(w)
        narrow(w, "clinic_manager", permission)
        with w.acting(w.other_manager):
            refused(_refuses("step authority denied"), _raw_step(w, run, "cancelled"))

    return case


CONDITIONS: dict[str, Callable[[World], None]] = {
    "invalid task#1:permission:tasks.assign": task_without_assign,
    "invalid task#2:actor:NEW.created_by_id IS DISTINCT FROM actor": invalid_task,
    "task assignment denied#1:helper:workflows_owner_valid": (
        assignment_to_an_invalid_owner
    ),
    "task assignment denied#2:permission:tasks.reassign": assignment,
    "task assignment denied#3:actor:NEW.owner_user_id IS NOT DISTINCT FROM actor": (
        assignment_to_a_role_without_reassign
    ),
    "task completion denied#1:permission:tasks.complete": start_without_complete,
    "task completion denied#2:helper:workflows_owned": completion,
    "task cancellation denied#1:permission:tasks.reassign": cancellation,
    "invalid escalation#1:permission:tasks.assign": escalate_without_assign,
    "comment denied#1:actor:NEW.author_id IS DISTINCT FROM actor": comment_trigger,
    "comment denied#2:permission:tasks.view": comment_without_view,
    "comment denied#3:actor:t.created_by_id=actor": comment_on_anothers_open_task,
    "comment denied#4:helper:workflows_owned": comment_trigger,
    "comment denied#5:permission:tasks.reassign": comment_trigger,
    "definition denied#1:actor:NEW.published_by_id IS DISTINCT FROM actor": (
        definition_trigger
    ),
    "definition denied#2:permission:tasks.reassign": _manager_without(
        "tasks.reassign", raw_definition, "definition denied"
    ),
    "definition denied#3:permission:tasks.view": _manager_without(
        "tasks.view", raw_definition, "definition denied"
    ),
    "run denied#1:actor:NEW.started_by_id IS DISTINCT FROM actor": run_trigger,
    "run denied#2:permission:tasks.reassign": _manager_without(
        "tasks.reassign", raw_run, "run denied"
    ),
    "run denied#3:permission:tasks.assign": _manager_without(
        "tasks.assign", raw_run, "run denied"
    ),
    "run denied#4:permission:tasks.view": _manager_without(
        "tasks.view", raw_run, "run denied"
    ),
    "run transition denied#1:permission:tasks.reassign": transition_without_reassign,
    "run transition denied#2:permission:tasks.assign": run_transition,
    "step authority denied#1:actor:run.started_by_id=actor": step_by_another_manager,
    "step authority denied#2:permission:tasks.reassign": _starter_step_without(
        "tasks.reassign"
    ),
    "step authority denied#3:permission:tasks.assign": _starter_step_without(
        "tasks.assign"
    ),
    "step authority denied#4:permission:tasks.view": _starter_step_without(
        "tasks.view"
    ),
    "step authority denied#5:permission:tasks.reassign": _cancel_step_without(
        "tasks.reassign"
    ),
    "step authority denied#6:permission:tasks.view": _cancel_step_without("tasks.view"),
}


def _condition_key(cid: str) -> str:
    return cid.removeprefix(GUARD)


def test_every_trigger_condition_has_a_missing_input_case() -> None:
    """Fail closed: each derived branch condition has its own refusal case."""
    assert {_condition_key(cid) for cid in discover_conditions()} == set(CONDITIONS)


@pytest.mark.parametrize("condition", sorted(CONDITIONS))
def test_trigger_condition_refuses_when_its_input_is_missing(
    world: World, condition: str
) -> None:
    CONDITIONS[condition](world)


@pytest.mark.parametrize(
    ("surface", "ddl"),
    [
        (
            "constraint",
            "ALTER TABLE clinic_app.workflows_taskcomment ADD CONSTRAINT "
            "workflows_plant CHECK (author_id = NULLIF(current_setting("
            "'app.current_user_id', true), '')::uuid) NOT VALID",
        ),
        (
            "default",
            "ALTER TABLE clinic_app.workflows_task ALTER COLUMN owner_role SET "
            "DEFAULT current_setting('app.current_user_id', true)",
        ),
        (
            "view",
            "CREATE VIEW clinic_app.workflows_plant AS SELECT t.id FROM "
            "clinic_app.workflows_task t WHERE t.created_by_id = NULLIF("
            "current_setting('app.current_user_id', true), '')::uuid",
        ),
        (
            "helper",
            "ALTER TABLE clinic_app.workflows_task ADD CONSTRAINT workflows_plant "
            "CHECK (clinic_app.has_permission('tasks.view', clinic_id, NULL)) "
            "NOT VALID",
        ),
    ],
)
def test_unclassified_actor_surface_fails_the_inventory(surface: str, ddl: str) -> None:
    """Plants: an actor read in a constraint, default or view is refused."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(ddl)
        with pytest.raises(AssertionError, match="unclassified actor-reading surface"):
            discover_sql_sites()
        transaction.set_rollback(True)
    assert set(discover_sql_sites()) == set(CASES), surface


def test_every_nonmanager_role_sees_and_comments_only_on_work_it_owns(
    world: World,
) -> None:
    """Role ownership through real RLS for every non-manager staff role."""
    assert len(NON_MANAGERS) >= 2
    staff = {role: permission_actor(world.graph, role)[0] for role in NON_MANAGERS}
    with runtime_role(), tenant_context(world.manager, world.graph.organization_a):
        owned_by = {
            role: assign_task(
                clinic_id=world.clinic,
                task_id=create_task(
                    clinic_id=world.clinic,
                    spec=spec(world.graph),
                    idempotency_key=uuid4(),
                ).pk,
                owner=TaskOwner(role=role),
                expected_revision=1,
            ).pk
            for role in NON_MANAGERS
        }
    for role, user in staff.items():
        with runtime_role(), tenant_context(user, world.graph.organization_a):
            own = create_task(
                clinic_id=world.clinic, spec=spec(world.graph), idempotency_key=uuid4()
            ).pk
            listed = {task.pk for task in list_tasks(clinic_id=world.clinic)}
            inherited = {world.role_task.pk} if role == "nurse" else set()
            assert listed == {own, owned_by[role], *inherited}, role
            for other, task in owned_by.items():
                if other == role:
                    require_task_access(
                        clinic_id=world.clinic, task_id=task, permission="tasks.view"
                    )
                    add_comment(
                        clinic_id=world.clinic,
                        task_id=task,
                        body="Sintetico nota",
                        idempotency_key=uuid4(),
                    )
                    continue
                with pytest.raises(WorkflowAccessDeniedError):
                    require_task_access(
                        clinic_id=world.clinic, task_id=task, permission="tasks.view"
                    )
                with pytest.raises(WorkflowAccessDeniedError):
                    add_comment(
                        clinic_id=world.clinic,
                        task_id=task,
                        body="Sintetico nota",
                        idempotency_key=uuid4(),
                    )
