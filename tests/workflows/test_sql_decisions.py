"""Every derived SQL decision refuses and allows through real RLS as clinic_app.

``sql_sites.discover_sql_sites`` derives the functions, policies and trigger
branches that read the actor. Each has one behavioural case here that makes
it decide both ways. Direct row writes bypass the Python services on purpose:
these decisions must hold on their own. The SQL mutant sweep forces each site
to allow and to deny and requires one of these tests to fail.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import time, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.inbox_services import open_conversation, send_message
from apps.comms.portal_messages import reply_to_portal_conversation
from apps.ehr.models import ClinicalDocument
from apps.identity.models import CareTeamMembership, RoleGrant, User
from apps.identity.permissions import BUNDLES_V2
from apps.intake.patient_access import patient_session_context
from apps.orders.review import release_result as orders_release
from apps.tenancy.db import tenant_context
from apps.workflows.access import WorkflowAccessDeniedError, require_task_access
from apps.workflows.care import services as care
from apps.workflows.care.models import (
    CareAction,
    CarePlan,
    CareRefusal,
    PROAssignment,
    PROOccurrence,
    Recall,
    Referral,
)
from apps.workflows.care.validation import CareActionSpec, PROSpec, ReferralSpec
from apps.workflows.models import (
    Task,
    TaskComment,
    WorkflowDefinitionVersion,
    WorkflowRun,
    WorkflowStep,
)
from apps.workflows.release_models import ReleaseDelivery, ReleaseNoticeTemplate
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    add_comment,
    assign_task,
    cancel_run,
    create_task,
    list_tasks,
    publish_definition,
    start_run,
)
from apps.workflows.urgent_models import UrgentConcern, UrgentCoveragePolicy
from django.db import DatabaseError, connection, transaction
from django.db.models import F, Model
from django.utils import timezone

from ehr.longitudinal_support import ChartWorld, finalized, second_draft
from identity.permission_support import (
    owner_context,
    permission_context,
)
from patient_service_support import runtime_role
from workflows.clock_support import database_clock
from workflows.coordination_support import (
    NOTICE,
    ROUTINE,
    URGENT,
    acknowledged,
    bundle_v2,
    patient_reply,
    urgent_world,
)
from workflows.permission_support import permission_actor
from workflows.sql_sites import discover_conditions, discover_sql_sites
from workflows.test_care_privacy import CALM, care_clinician
from workflows.test_careplans import amended, instruction, return_in, rows
from workflows.test_careplans import world as care_chart
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from datetime import datetime

    from apps.ehr.models import ClinicalDocumentVersion

    from rbac_fixtures import RbacGraph
    from workflows.coordination_support import UrgentWorld

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
    world: World,
    creator: UUID,
    *,
    due: timedelta = timedelta(hours=1),
    depends_on: UUID | None = None,
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
        depends_on_id=depends_on,
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


def raw_definition(world: World, creator: UUID) -> WorkflowDefinitionVersion:
    return WorkflowDefinitionVersion.objects.create(
        organization_id=world.graph.organization_a,
        clinic_id=world.clinic,
        key=f"sql-{uuid4().hex[:12]}",
        version=1,
        steps=TIMER,
        created_by_id=creator,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
        creation_fingerprint="0" * 64,
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
        assert (
            WorkflowRun.objects.filter(pk=w.run.pk).update(
                state="running", revision=F("revision") + 1
            )
            == 1
        )
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        refused(
            ("42501", "run transition denied"),
            lambda: WorkflowRun.objects.filter(pk=w.run.pk).update(
                state="waiting", revision=F("revision") + 1
            ),
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


def definition_command(definition: UUID, **fields: object) -> int:
    return WorkflowDefinitionVersion.objects.filter(pk=definition).update(
        revision=F("revision") + 1,
        last_command_key=uuid4(),
        last_command_digest="a" * 64,
        **fields,
    )


def definition_authority(w: World) -> None:
    with w.acting(w.nurse):
        refused(("23514", "definition denied"), lambda: raw_definition(w, w.nurse))
    with w.acting(w.manager):
        raw_definition(w, w.manager)


def definition_update(w: World) -> None:
    with w.acting(w.manager):
        draft = raw_definition(w, w.manager)
    narrow(w, "nurse", "tasks.view")
    with w.acting(w.nurse):
        assert (
            WorkflowDefinitionVersion.objects.filter(pk=draft.pk).update(
                state="validated"
            )
            == 0
        )
        assert definition_command(draft.pk, state="validated") == 0
    with w.acting(w.manager):
        assert definition_command(draft.pk, state="validated") == 1


def publication_actor(w: World) -> None:
    with w.acting(w.manager):
        draft = raw_definition(w, w.manager)
        assert definition_command(draft.pk, state="validated") == 1
        refused(
            ("23514", "publication changed content"),
            lambda: definition_command(
                draft.pk, state="published", published_by_id=w.other_manager
            ),
        )
        assert (
            definition_command(draft.pk, state="published", published_by_id=w.manager)
            == 1
        )


def run_retry_actor(w: World) -> None:
    with w.acting(w.manager):
        assert (
            WorkflowRun.objects.filter(pk=w.run.pk).update(
                state="running", revision=F("revision") + 1
            )
            == 1
        )
        assert (
            WorkflowStep.objects.filter(pk=w.step.pk).update(
                state="running",
                fencing_token=1,
                claim_until=timezone.now() + timedelta(minutes=2),
            )
            == 1
        )
        assert (
            WorkflowStep.objects.filter(pk=w.step.pk).update(
                state="failed", error_code="handler_rejected", claim_until=None
            )
            == 1
        )
        assert (
            WorkflowRun.objects.filter(pk=w.run.pk).update(
                state="failed", revision=F("revision") + 1
            )
            == 1
        )

    def retry() -> int:
        return WorkflowRun.objects.filter(pk=w.run.pk).update(
            state="pending",
            revision=F("revision") + 1,
            last_command_key=uuid4(),
            last_command_digest="a" * 64,
        )

    with w.acting(w.other_manager):
        refused(("23514", "run retry denied"), retry)
    with w.acting(w.manager):
        assert retry() == 1


def predecessor_ready(w: World) -> None:
    query = "SELECT clinic_app.workflows_predecessor_ready(%s,%s)"
    with w.acting(w.nurse):
        parent = raw_task(w, w.nurse)
        assert command(parent.pk, state="assigned", owner_user_id=w.nurse) == 1
        child = raw_task(w, w.nurse, depends_on=parent.pk)
        assert command(child.pk, state="assigned", owner_user_id=w.nurse) == 1
        assert scalar(query, [w.clinic, child.pk]) is False
        assert command(parent.pk, state="in_progress") == 1
        assert (
            command(parent.pk, state="done", completion_evidence={"checked": True}) == 1
        )
        assert scalar(query, [w.clinic, child.pk]) is True
    with w.acting(w.receptionist):
        assert scalar(query, [w.clinic, child.pk]) is False
    narrow(w, "nurse", "tasks.complete")
    with w.acting(w.nurse):
        assert scalar(query, [w.clinic, child.pk]) is False


# --- Care decisions (todo 52) ---------------------------------------------------
# The care guard, its two resolvers and its row policies decide on the actor.
# Rows are written directly as clinic_app, so the database decides alone. The
# allowed write runs in a savepoint that is rolled back, so the same row then
# shows the refusal once exactly one input is removed (a permission narrowed or
# an actor column forged).


@dataclass(frozen=True)
class Care:
    w: World
    chart: ChartWorld
    clinician: UUID
    enrollment: UUID
    version: ClinicalDocumentVersion
    plan: CarePlan
    window: CareAction
    late: CareAction
    note: CareAction
    referral: Referral

    @property
    def organization(self) -> UUID:
        return self.w.graph.organization_a


def care_world(w: World) -> Care:
    """A v2 care-team physician's plan: window, late, instruction and referral."""
    chart, version = care_chart(w.graph)
    clinician = care_clinician(w.graph, "physician", chart.enrollment)
    now = timezone.now()
    actions = (
        return_in(6),
        CareActionSpec(
            kind="staff_coordination",
            window_start=now - timedelta(days=3),
            window_end=now - timedelta(days=1),
        ),
        instruction(),
        CareActionSpec(
            kind="referral",
            window_start=now,
            window_end=now + timedelta(days=14),
            referral=ReferralSpec(target_kind="specialty", target_label="Cardiologia"),
        ),
    )
    with w.acting(clinician):
        plan = care.create_care_plan_from_version(
            clinic_id=w.clinic,
            version_id=version.pk,
            actions=actions,
            idempotency_key=uuid4(),
        )
    kinds = {a.kind: a for a in rows(w.graph, CareAction, plan_id=plan.pk)}
    (referral,) = rows(w.graph, Referral, plan_id=plan.pk)
    return Care(
        w,
        chart,
        clinician,
        chart.enrollment,
        version,
        plan,
        kinds["return_window"],
        kinds["staff_coordination"],
        kinds["patient_instruction"],
        referral,
    )


def allowed(action: Callable[[], object]) -> object:
    """A write that must succeed, rolled back to its savepoint afterwards."""
    with transaction.atomic():
        result = action()
        transaction.set_rollback(True)
    return result


def care_command(model: type[Model], pk: UUID, **fields: object) -> int:
    """One revisioned care write, exactly as a care service issues it."""
    return model._default_manager.filter(pk=pk).update(
        revision=F("revision") + 1,
        last_command_key=uuid4(),
        last_command_digest="a" * 64,
        **fields,
    )


def care_visible(model: type[Model]) -> set[UUID]:
    return set(model._default_manager.values_list("pk", flat=True))


def spare_note(c: Care) -> tuple[UUID, UUID]:
    """Another finalized note on the chart: (version, encounter), no plan yet."""
    version = finalized(
        c.w.graph, second_draft(c.w.graph, c.chart, c.version.author_id)
    )
    (document,) = rows(c.w.graph, ClinicalDocument, pk=version.document_id)
    return version.pk, document.encounter_id


def raw_plan(c: Care, clinician: UUID, note: tuple[UUID, UUID]) -> CarePlan:
    return CarePlan.objects.create(
        organization_id=c.organization,
        clinic_id=c.w.clinic,
        enrollment_id=c.enrollment,
        encounter_id=note[1],
        source_version_id=note[0],
        clinician_id=clinician,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def raw_care_action(  # noqa: PLR0913 - one column per keyword
    c: Care,
    creator: UUID,
    *,
    approver: UUID | None,
    kind: str = "patient_instruction",
    task: UUID | None = None,
    identifier: UUID | None = None,
    end: datetime | None = None,
) -> CareAction:
    now = timezone.now()
    return CareAction.objects.create(
        id=identifier or uuid4(),
        organization_id=c.organization,
        clinic_id=c.w.clinic,
        plan_id=c.plan.pk,
        kind=kind,
        window_start=now,
        window_end=end or now + timedelta(days=3),
        origin_version_id=c.plan.source_version_id,
        approved_by_id=approver,
        instruction="Repouso relativo." if kind == "patient_instruction" else None,
        task_id=task,
        created_by_id=creator,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def follow_up(c: Care, actor: UUID, action: UUID, due: datetime) -> UUID:
    """The open follow-up task a staff action must be bound to."""
    with c.w.acting(actor):
        return create_task(
            clinic_id=c.w.clinic,
            spec=TaskSpec(
                kind="follow_up",
                subject_ref={"kind": "enrollment", "id": str(c.enrollment)},
                due_at=due,
            ),
            idempotency_key=action,
        ).pk


def staff_action(c: Care) -> tuple[UUID, UUID, datetime]:
    identifier, due = uuid4(), timezone.now() + timedelta(days=3)
    return identifier, follow_up(c, c.w.manager, identifier, due), due


def raw_referral(c: Care, action: UUID, creator: UUID) -> Referral:
    return Referral.objects.create(
        organization_id=c.organization,
        clinic_id=c.w.clinic,
        plan_id=c.plan.pk,
        action_id=action,
        target_kind="specialty",
        target_label="Cardiologia",
        status="requested",
        created_by_id=creator,
    )


def referral_action(c: Care) -> UUID:
    """A clinician's referral action that has no coordination row yet."""
    identifier, due = uuid4(), timezone.now() + timedelta(days=3)
    task = follow_up(c, c.clinician, identifier, due)
    with c.w.acting(c.clinician):
        raw_care_action(
            c,
            c.clinician,
            approver=c.clinician,
            kind="referral",
            task=task,
            identifier=identifier,
            end=due,
        )
    return identifier


def raw_recall(c: Care, creator: UUID) -> Recall:
    now = timezone.now()
    return Recall.objects.create(
        organization_id=c.organization,
        clinic_id=c.w.clinic,
        enrollment_id=c.enrollment,
        reason="routine_return",
        interval_days=30,
        anchor_at=now,
        due_at=now + timedelta(days=30),
        owner_role="receptionist",
        created_by_id=creator,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def raw_series(c: Care, creator: UUID, occurrences: int = 1) -> PROAssignment:
    return PROAssignment.objects.create(
        organization_id=c.organization,
        clinic_id=c.w.clinic,
        enrollment_id=c.enrollment,
        instrument_key="phq9",
        instrument_version=1,
        first_due_at=timezone.now() + timedelta(days=7),
        interval_days=7,
        occurrences=occurrences,
        response_role="physician",
        staffing_policy_ref="eg3-pending-staffing-policy",
        created_by_id=creator,
        idempotency_key=uuid4(),
        fingerprint="0" * 64,
    )


def raw_slot(c: Care, series: PROAssignment, creator: UUID) -> PROOccurrence:
    return PROOccurrence.objects.create(
        organization_id=c.organization,
        clinic_id=c.w.clinic,
        assignment=series,
        sequence=1,
        due_at=series.first_due_at,
        created_by_id=creator,
    )


def pending_slot(c: Care) -> PROOccurrence:
    """One pending slot of a series the clinician assigned through the service."""
    with c.w.acting(c.clinician):
        series = care.assign_pro(
            clinic_id=c.w.clinic,
            enrollment_id=c.enrollment,
            spec=pro_spec(),
            idempotency_key=uuid4(),
        )
    (slot,) = rows(c.w.graph, PROOccurrence, assignment_id=series.pk)
    return slot


def pro_spec() -> PROSpec:
    return PROSpec(
        instrument_key="phq9",
        first_due_at=timezone.now() + timedelta(days=1),
        interval_days=7,
        occurrences=1,
        response_role="physician",
        staffing_policy_ref="eg3-pending-staffing-policy",
    )


def respond(slot: PROOccurrence) -> int:
    return care_command(
        PROOccurrence,
        slot.pk,
        state="responded",
        answers={"instrument": "phq9", "answers": CALM, "score": 0},
        response_task_id=uuid4(),
        recorded_at=timezone.now(),
    )


def superseded(c: Care) -> None:
    """Amend the plan's source and supersede the plan, as the scanner does."""
    amended(c.w.graph, c.version)
    with c.w.acting(c.w.manager):
        assert care_command(CarePlan, c.plan.pk, state="superseded") == 1


# Branches: each refuses on its decision and allows the same write otherwise.


def care_plan_insert(w: World) -> None:
    c = care_world(w)
    note = spare_note(c)
    with w.acting(c.clinician):
        allowed(lambda: raw_plan(c, c.clinician, note))
        refused(("23514", "invalid care plan"), lambda: raw_plan(c, w.manager, note))
    with w.acting(w.manager):
        refused(("23514", "invalid care plan"), lambda: raw_plan(c, w.manager, note))


def care_plan_by_another(w: World) -> None:
    c = care_world(w)
    note = spare_note(c)
    with w.acting(c.clinician):
        refused(("23514", "invalid care plan"), lambda: raw_plan(c, w.nurse, note))
        allowed(lambda: raw_plan(c, c.clinician, note))


def care_plan_without_write(w: World) -> None:
    c = care_world(w)
    note = spare_note(c)
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(("23514", "invalid care plan"), lambda: raw_plan(c, c.clinician, note))


def care_plan_transition(w: World) -> None:
    c = care_world(w)
    amended(w.graph, c.version)

    def supersede() -> int:
        return care_command(CarePlan, c.plan.pk, state="superseded")

    with w.acting(w.manager):
        assert allowed(supersede) == 1
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        refused(("23514", "invalid care plan transition"), supersede)


def care_action_insert(w: World) -> None:
    c = care_world(w)
    with w.acting(c.clinician):
        allowed(lambda: raw_care_action(c, c.clinician, approver=c.clinician))
        refused(
            ("23514", "invalid care action"),
            lambda: raw_care_action(c, w.manager, approver=c.clinician),
        )


def care_action_without_view(w: World) -> None:
    c = care_world(w)
    narrow(w, "physician", "tasks.view")
    with w.acting(c.clinician):
        refused(
            ("23514", "invalid care action"),
            lambda: raw_care_action(c, c.clinician, approver=c.clinician),
        )


def staff_action_without_assign(w: World) -> None:
    c = care_world(w)
    identifier, task, due = staff_action(c)

    def insert() -> CareAction:
        return raw_care_action(
            c,
            w.manager,
            approver=None,
            kind="staff_coordination",
            task=task,
            identifier=identifier,
            end=due,
        )

    with w.acting(w.manager):
        allowed(insert)
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        refused(("42501", "care action denied"), insert)


def clinical_action_approval(w: World) -> None:
    c = care_world(w)
    message = ("42501", "care action requires clinician approval")
    with w.acting(c.clinician):
        allowed(lambda: raw_care_action(c, c.clinician, approver=c.clinician))
        refused(message, lambda: raw_care_action(c, c.clinician, approver=w.manager))
    with w.acting(w.manager):
        refused(message, lambda: raw_care_action(c, w.manager, approver=w.manager))


def clinical_action_by_another(w: World) -> None:
    c = care_world(w)
    with w.acting(c.clinician):
        refused(
            ("42501", "care action requires clinician approval"),
            lambda: raw_care_action(c, c.clinician, approver=w.nurse),
        )


def clinical_action_without_write(w: World) -> None:
    c = care_world(w)
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(
            ("42501", "care action requires clinician approval"),
            lambda: raw_care_action(c, c.clinician, approver=c.clinician),
        )


def overdue_marking(w: World) -> None:
    c = care_world(w)

    def mark() -> int:
        return care_command(CareAction, c.late.pk, state="overdue")

    with w.acting(w.manager):
        assert allowed(mark) == 1
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        refused(("23514", "invalid overdue marking"), mark)


def clinician_decision(w: World) -> None:
    c = care_world(w)

    def cancel() -> int:
        return care_command(CareAction, c.note.pk, state="cancelled")

    with w.acting(c.clinician):
        assert allowed(cancel) == 1
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(("42501", "clinician decision required"), cancel)


def review_flag(w: World) -> None:
    c = care_world(w)
    superseded(c)

    def flag() -> int:
        return care_command(CareAction, c.window.pk, review_state="required")

    with w.acting(w.manager):
        assert allowed(flag) == 1
    narrow(w, "clinic_manager", "tasks.assign")
    with w.acting(w.manager):
        refused(("42501", "review flag denied"), flag)


def review_decision(w: World) -> None:
    c = care_world(w)
    superseded(c)
    with w.acting(w.manager):
        assert care_command(CareAction, c.window.pk, review_state="required") == 1

    def confirm() -> int:
        return care_command(CareAction, c.window.pk, review_state="confirmed")

    with w.acting(c.clinician):
        assert allowed(confirm) == 1
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(("42501", "review decision denied"), confirm)


def referral_insert(w: World) -> None:
    c = care_world(w)
    action = referral_action(c)
    with w.acting(c.clinician):
        allowed(lambda: raw_referral(c, action, c.clinician))
        refused(("23514", "invalid referral"), lambda: raw_referral(c, action, w.nurse))


def referral_without_write(w: World) -> None:
    c = care_world(w)
    action = referral_action(c)
    with w.acting(c.clinician):
        allowed(lambda: raw_referral(c, action, c.clinician))
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(
            ("42501", "invalid referral"), lambda: raw_referral(c, action, c.clinician)
        )


def referral_link(w: World) -> None:
    c = care_world(w)
    from apps.prescription.models import DocumentKind  # noqa: PLC0415
    from apps.prescription.services import create_document_draft  # noqa: PLC0415

    with runtime_role(), tenant_context(c.version.author_id, w.graph.organization_a):
        draft = create_document_draft(
            clinic_id=w.clinic,
            encounter_id=c.plan.encounter_id,
            kind=DocumentKind.REFERRAL,
        )

    def link() -> int:
        return care_command(Referral, c.referral.pk, document_id=draft.pk)

    with w.acting(c.clinician):
        assert allowed(link) == 1
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(("42501", "referral link denied"), link)


def referral_transition(w: World) -> None:
    c = care_world(w)

    def schedule() -> int:
        return care_command(Referral, c.referral.pk, status="scheduled")

    with w.acting(c.clinician):
        assert allowed(schedule) == 1
    narrow(w, "physician", "tasks.assign")
    with w.acting(c.clinician):
        refused(("23514", "invalid referral transition"), schedule)


def recall_insert(w: World) -> None:
    c = care_world(w)
    with w.acting(w.manager):
        allowed(lambda: raw_recall(c, w.manager))
        refused(("23514", "invalid recall"), lambda: raw_recall(c, w.other_manager))


def recall_without_reassign(w: World) -> None:
    c = care_world(w)
    with w.acting(w.nurse):
        refused(("23514", "invalid recall"), lambda: raw_recall(c, w.nurse))
    with w.acting(w.manager):
        allowed(lambda: raw_recall(c, w.manager))


def recall_transition(w: World) -> None:
    c = care_world(w)
    start = timezone.now()
    clock = {"now": start}
    with database_clock(w.graph.organization_a, lambda: clock["now"]) as advance:
        with w.acting(w.manager):
            recall = care.schedule_recall(
                clinic_id=w.clinic,
                enrollment_id=c.enrollment,
                reason="routine_return",
                interval_days=1,
                owner_role="receptionist",
                idempotency_key=uuid4(),
            )
            task = create_task(
                clinic_id=w.clinic,
                spec=TaskSpec(
                    kind="follow_up",
                    subject_ref={"kind": "enrollment", "id": str(c.enrollment)},
                    due_at=recall.due_at,
                ),
                idempotency_key=recall.pk,
            )
            assign_task(
                clinic_id=w.clinic,
                task_id=task.pk,
                owner=TaskOwner(role="receptionist"),
                expected_revision=1,
            )
        clock["now"] = start + timedelta(days=2)
        advance()

        def materialize() -> int:
            return care_command(
                Recall, recall.pk, state="materialized", task_id=task.pk
            )

        with w.acting(w.manager):
            assert allowed(materialize) == 1
        narrow(w, "clinic_manager", "tasks.reassign")
        with w.acting(w.manager):
            refused(("23514", "invalid recall transition"), materialize)


def pro_assignment_insert(w: World) -> None:
    c = care_world(w)
    with w.acting(c.clinician):
        allowed(lambda: raw_series(c, c.clinician))
        refused(("23514", "invalid pro assignment"), lambda: raw_series(c, w.nurse))
    with w.acting(w.manager):
        refused(("23514", "invalid pro assignment"), lambda: raw_series(c, w.manager))


def pro_assignment_without_write(w: World) -> None:
    c = care_world(w)
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(("23514", "invalid pro assignment"), lambda: raw_series(c, c.clinician))


def pro_assignment_transition(w: World) -> None:
    c = care_world(w)
    with w.acting(c.clinician):
        series = raw_series(c, c.clinician)

    def complete() -> int:
        return care_command(PROAssignment, series.pk, state="completed")

    with w.acting(c.clinician):
        assert allowed(complete) == 1
    narrow(w, "physician", "tasks.assign")
    with w.acting(c.clinician):
        refused(("23514", "invalid pro assignment transition"), complete)


def pro_slot_insert(w: World) -> None:
    c = care_world(w)
    with w.acting(c.clinician):
        series = raw_series(c, c.clinician, occurrences=2)
        allowed(lambda: raw_slot(c, series, c.clinician))
        refused(
            ("23514", "invalid pro occurrence"), lambda: raw_slot(c, series, w.nurse)
        )
    with w.acting(w.manager):
        refused(
            ("23514", "invalid pro occurrence"), lambda: raw_slot(c, series, w.manager)
        )


def pro_slot_without_write(w: World) -> None:
    c = care_world(w)
    with w.acting(c.clinician):
        series = raw_series(c, c.clinician, occurrences=2)
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(
            ("23514", "invalid pro occurrence"),
            lambda: raw_slot(c, series, c.clinician),
        )


def pro_response(w: World) -> None:
    c = care_world(w)
    slot = pending_slot(c)
    with w.acting(c.clinician):
        assert allowed(lambda: respond(slot)) == 1
    narrow(w, "physician", "clinical.write")
    with w.acting(c.clinician):
        refused(("42501", "pro response denied"), lambda: respond(slot))


def pro_response_without_observation(w: World) -> None:
    # A nurse's only route to the slot is observation.write: it both shows the
    # row (RLS) and admits the write (guard), so narrowing it leaves nothing
    # to update. The refusal is therefore the empty update, not a guard error.
    c = care_world(w)
    slot = pending_slot(c)
    nurse = care_clinician(w.graph, "nurse", c.enrollment)
    with w.acting(nurse):
        assert allowed(lambda: respond(slot)) == 1
    narrow(w, "nurse", "observation.write")
    with w.acting(nurse):
        assert respond(slot) == 0


def pro_nonresponse(w: World) -> None:
    c = care_world(w)
    slot = pending_slot(c)
    start = timezone.now()
    with database_clock(w.graph.organization_a, lambda: start + timedelta(days=10)):

        def close() -> int:
            return care_command(PROOccurrence, slot.pk, state="no_response")

        with w.acting(c.clinician):
            assert allowed(close) == 1
        narrow(w, "physician", "tasks.assign")
        with w.acting(c.clinician):
            refused(("23514", "pro nonresponse denied"), close)


# Resolvers: identity only after the caller's tasks.view in the clinic.


def care_version_scope(w: World) -> None:
    c = care_world(w)
    query = "SELECT count(*) FROM clinic_app.workflows_care_version_scope(%s,%s)"
    with w.acting(c.clinician):
        assert scalar(query, [w.clinic, c.version.pk]) == 1
    narrow(w, "physician", "tasks.view")
    with w.acting(c.clinician):
        assert scalar(query, [w.clinic, c.version.pk]) == 0


def care_occurrence_scope(w: World) -> None:
    c = care_world(w)
    slot = pending_slot(c)
    query = "SELECT clinic_app.workflows_care_occurrence_scope(%s,%s)"
    with w.acting(c.clinician):
        assert scalar(query, [w.clinic, slot.pk]) == c.enrollment
    narrow(w, "physician", "tasks.view")
    with w.acting(c.clinician):
        assert scalar(query, [w.clinic, slot.pk]) is None


# Policies: read decides visibility; update decides which rows reach the guard
# (a visible row meets the guard's command check, an invisible one is skipped).

UNBUMPED = ("23514", "care command identity is invalid")


def _touch(table: str) -> Callable[[], int]:
    return lambda: unscoped(f"UPDATE clinic_app.{table} SET updated_at=now()")  # noqa: S608


def care_plan_read(w: World) -> None:
    c = care_world(w)
    with w.acting(w.receptionist):
        assert care_visible(CarePlan) == set()
    with w.acting(w.manager):
        assert care_visible(CarePlan) == {c.plan.pk}


def care_plan_update(w: World) -> None:
    care_world(w)
    with w.acting(w.receptionist):
        assert _touch("workflows_careplan")() == 0
    with w.acting(w.manager):
        refused(UNBUMPED, _touch("workflows_careplan"))


def care_action_read(w: World) -> None:
    c = care_world(w)
    staff = {c.window.pk, c.late.pk}
    with w.acting(w.receptionist):
        assert care_visible(CareAction) == set()
    with w.acting(w.manager):
        visible_actions = care_visible(CareAction)
        assert staff <= visible_actions
        assert c.note.pk not in visible_actions
    with w.acting(c.clinician):
        assert {*staff, c.note.pk} <= care_visible(CareAction)


def care_action_update(w: World) -> None:
    care_world(w)
    with w.acting(w.receptionist):
        assert _touch("workflows_careaction")() == 0
    with w.acting(w.manager):
        refused(UNBUMPED, _touch("workflows_careaction"))


def care_referral_read(w: World) -> None:
    c = care_world(w)
    with w.acting(w.manager):
        assert care_visible(Referral) == set()
    with w.acting(c.clinician):
        assert care_visible(Referral) == {c.referral.pk}


def care_referral_update(w: World) -> None:
    c = care_world(w)
    with w.acting(w.manager):
        assert _touch("workflows_referral")() == 0
    with w.acting(c.clinician):
        refused(UNBUMPED, _touch("workflows_referral"))


def care_recall_read(w: World) -> None:
    c = care_world(w)
    with w.acting(w.manager):
        recall = raw_recall(c, w.manager)
    with w.acting(w.nurse):
        assert care_visible(Recall) == set()
    with w.acting(w.other_manager):
        assert care_visible(Recall) == {recall.pk}


def care_recall_update(w: World) -> None:
    c = care_world(w)
    with w.acting(w.manager):
        raw_recall(c, w.manager)
    with w.acting(w.nurse):
        assert _touch("workflows_recall")() == 0
    with w.acting(w.manager):
        refused(UNBUMPED, _touch("workflows_recall"))


def care_series_read(w: World) -> None:
    c = care_world(w)
    slot = pending_slot(c)
    with w.acting(w.manager):
        assert care_visible(PROAssignment) == set()
    with w.acting(c.clinician):
        assert care_visible(PROAssignment) == {slot.assignment_id}


def care_series_update(w: World) -> None:
    c = care_world(w)
    pending_slot(c)
    with w.acting(w.manager):
        assert _touch("workflows_proassignment")() == 0
    with w.acting(c.clinician):
        refused(UNBUMPED, _touch("workflows_proassignment"))


def care_slot_read(w: World) -> None:
    c = care_world(w)
    slot = pending_slot(c)
    with w.acting(w.manager):
        assert care_visible(PROOccurrence) == set()
    with w.acting(c.clinician):
        assert care_visible(PROOccurrence) == {slot.pk}


def care_slot_update(w: World) -> None:
    c = care_world(w)
    pending_slot(c)
    with w.acting(w.manager):
        assert _touch("workflows_prooccurrence")() == 0
    with w.acting(c.clinician):
        refused(UNBUMPED, _touch("workflows_prooccurrence"))


def care_refusal_read(w: World) -> None:
    c = care_world(w)
    with owner_context(w.graph.organization_a):
        refusal = CareRefusal.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            kind="overdue",
            record_id=c.late.pk,
            actor_id=c.clinician,
            refused_at=timezone.now(),
            retry_at=timezone.now() + timedelta(minutes=60),
        )
    with w.acting(c.clinician):
        assert care_visible(CareRefusal) == set()
    with w.acting(w.manager):
        assert care_visible(CareRefusal) == {refusal.pk}


CARE_GUARD = "branch:workflows_care_guard():"
CARE_CASES: dict[str, Callable[[World], None]] = {
    "policy:workflows_careplan.care_read": care_plan_read,
    "policy:workflows_careplan.care_update": care_plan_update,
    "policy:workflows_careaction.care_read": care_action_read,
    "policy:workflows_careaction.care_update": care_action_update,
    "policy:workflows_referral.care_read": care_referral_read,
    "policy:workflows_referral.care_update": care_referral_update,
    "policy:workflows_recall.care_read": care_recall_read,
    "policy:workflows_recall.care_update": care_recall_update,
    "policy:workflows_proassignment.care_read": care_series_read,
    "policy:workflows_proassignment.care_update": care_series_update,
    "policy:workflows_prooccurrence.care_read": care_slot_read,
    "policy:workflows_prooccurrence.care_update": care_slot_update,
    "policy:workflows_carerefusal.care_read": care_refusal_read,
    "function:workflows_care_version_scope(uuid,uuid)": care_version_scope,
    "function:workflows_care_occurrence_scope(uuid,uuid)": care_occurrence_scope,
    CARE_GUARD + "invalid care plan": care_plan_insert,
    CARE_GUARD + "invalid care plan transition": care_plan_transition,
    CARE_GUARD + "invalid care action": care_action_insert,
    CARE_GUARD + "care action denied": staff_action_without_assign,
    CARE_GUARD + "care action requires clinician approval": clinical_action_approval,
    CARE_GUARD + "invalid overdue marking": overdue_marking,
    CARE_GUARD + "clinician decision required": clinician_decision,
    CARE_GUARD + "review flag denied": review_flag,
    CARE_GUARD + "review decision denied": review_decision,
    CARE_GUARD + "invalid referral": referral_insert,
    CARE_GUARD + "invalid referral#2": referral_without_write,
    CARE_GUARD + "referral link denied": referral_link,
    CARE_GUARD + "invalid referral transition": referral_transition,
    CARE_GUARD + "invalid recall": recall_insert,
    CARE_GUARD + "invalid recall transition": recall_transition,
    CARE_GUARD + "invalid pro assignment": pro_assignment_insert,
    CARE_GUARD + "invalid pro assignment transition": pro_assignment_transition,
    CARE_GUARD + "invalid pro occurrence": pro_slot_insert,
    CARE_GUARD + "pro response denied": pro_response,
    CARE_GUARD + "pro nonresponse denied": pro_nonresponse,
}
CARE_CONDITIONS: dict[str, Callable[[World], None]] = {
    CARE_GUARD + "care action denied#1:permission:tasks.assign": (
        staff_action_without_assign
    ),
    CARE_GUARD
    + "care action requires clinician approval#1:actor:"
    + "NEW.approved_by_id IS DISTINCT FROM actor": clinical_action_by_another,
    CARE_GUARD
    + "care action requires clinician approval#2:permission:clinical.write": (
        clinical_action_without_write
    ),
    CARE_GUARD + "clinician decision required#1:permission:clinical.write": (
        clinician_decision
    ),
    CARE_GUARD
    + "invalid care action#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        care_action_insert
    ),
    CARE_GUARD + "invalid care action#2:permission:tasks.view": (
        care_action_without_view
    ),
    CARE_GUARD + "invalid care plan transition#1:permission:tasks.assign": (
        care_plan_transition
    ),
    CARE_GUARD + "invalid care plan#1:actor:NEW.clinician_id IS DISTINCT FROM actor": (
        care_plan_by_another
    ),
    CARE_GUARD + "invalid care plan#2:permission:clinical.write": (
        care_plan_without_write
    ),
    CARE_GUARD + "invalid overdue marking#1:permission:tasks.assign": overdue_marking,
    CARE_GUARD + "invalid pro assignment transition#1:permission:tasks.assign": (
        pro_assignment_transition
    ),
    CARE_GUARD
    + "invalid pro assignment#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        pro_assignment_insert
    ),
    CARE_GUARD + "invalid pro assignment#2:permission:clinical.write": (
        pro_assignment_without_write
    ),
    CARE_GUARD
    + "invalid pro occurrence#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        pro_slot_insert
    ),
    CARE_GUARD + "invalid pro occurrence#2:permission:clinical.write": (
        pro_slot_without_write
    ),
    CARE_GUARD + "invalid recall transition#1:permission:tasks.reassign": (
        recall_transition
    ),
    CARE_GUARD + "invalid recall#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        recall_insert
    ),
    CARE_GUARD + "invalid recall#2:permission:tasks.reassign": recall_without_reassign,
    CARE_GUARD + "invalid referral transition#1:permission:tasks.assign": (
        referral_transition
    ),
    CARE_GUARD + "invalid referral#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        referral_insert
    ),
    CARE_GUARD + "invalid referral#2#1:permission:clinical.write": (
        referral_without_write
    ),
    CARE_GUARD + "pro nonresponse denied#1:permission:tasks.assign": pro_nonresponse,
    CARE_GUARD + "pro response denied#1:permission:observation.write": (
        pro_response_without_observation
    ),
    CARE_GUARD + "pro response denied#2:permission:clinical.write": pro_response,
    CARE_GUARD + "referral link denied#1:permission:clinical.write": referral_link,
    CARE_GUARD + "review decision denied#1:permission:clinical.write": review_decision,
    CARE_GUARD + "review flag denied#1:permission:tasks.assign": review_flag,
}


# --- Todo 55: release notices, deliveries, coverage and urgent concerns ---------

COORDINATION = "branch:workflows_coordination_guard():"


def _clinician(w: World) -> UUID:
    clinician, _enrollment = permission_actor(w.graph, "physician")
    return clinician


def _raw_notice(w: World, author: UUID) -> ReleaseNoticeTemplate:
    return ReleaseNoticeTemplate.objects.create(
        organization_id=w.graph.organization_a,
        clinic_id=w.clinic,
        version=1,
        body=NOTICE,
        approved_by_id=author,
    )


def _raw_coverage(w: World, author: UUID) -> UrgentCoveragePolicy:
    return UrgentCoveragePolicy.objects.create(
        organization_id=w.graph.organization_a,
        clinic_id=w.clinic,
        version=1,
        weekdays=[0, 1, 2, 3, 4],
        opens_at=time(8, 0),
        closes_at=time(18, 0),
        response_hours=4,
        responsible_roles=["nurse"],
        created_by_id=author,
    )


def coordination_notice_read(w: World) -> None:
    clinician = _clinician(w)
    with w.acting(clinician):
        notice = _raw_notice(w, clinician)
    with w.acting(w.receptionist):
        assert care_visible(ReleaseNoticeTemplate) == set()
    with w.acting(clinician):
        assert care_visible(ReleaseNoticeTemplate) == {notice.pk}


def coordination_notice_insert(w: World) -> None:
    clinician = _clinician(w)
    with w.acting(w.receptionist):
        refused(_refuses("notice refused"), lambda: _raw_notice(w, w.receptionist))
    with w.acting(clinician):
        assert _raw_notice(w, clinician).version == 1


def notice_by_another(w: World) -> None:
    clinician, other = _clinician(w), _clinician(w)
    with w.acting(clinician):
        refused(_refuses("notice refused"), lambda: _raw_notice(w, other))


def notice_without_release(w: World) -> None:
    clinician = _clinician(w)
    narrow(w, "physician", "configuration.propose")
    with w.acting(clinician):
        refused(_refuses("notice refused"), lambda: _raw_notice(w, clinician))


def coordination_coverage_read(w: World) -> None:
    owner, _enrollment = permission_actor(w.graph, "owner")
    with w.acting(w.manager):
        policy = _raw_coverage(w, w.manager)
    with w.acting(owner):
        assert care_visible(UrgentCoveragePolicy) == set()
    with w.acting(w.receptionist):
        assert care_visible(UrgentCoveragePolicy) == {policy.pk}


def coordination_coverage_insert(w: World) -> None:
    with w.acting(w.receptionist):
        refused(_refuses("coverage refused"), lambda: _raw_coverage(w, w.receptionist))
    with w.acting(w.manager):
        assert _raw_coverage(w, w.manager).version == 1


def coverage_by_another(w: World) -> None:
    with w.acting(w.manager):
        refused(_refuses("coverage refused"), lambda: _raw_coverage(w, w.other_manager))


def coverage_without_configuration(w: World) -> None:
    narrow(w, "clinic_manager", "configuration.clinic")
    with w.acting(w.manager):
        refused(_refuses("coverage refused"), lambda: _raw_coverage(w, w.manager))


def _delivery_fields(w: World) -> dict[str, object]:
    """A release and a portal notice by the clinician, with no delivery yet."""
    graph = w.graph
    bundle_v2(graph)
    chart, version = acknowledged(graph)
    with w.acting(graph.physician):
        release_row = orders_release(
            clinic_id=w.clinic, version_id=version, audience="patient"
        )
        thread = open_conversation(
            clinic_id=w.clinic,
            enrollment_id=chart.enrollment,
            channel="portal",
            purpose="care",
        ).conversation_id
        message = send_message(
            clinic_id=w.clinic, conversation_id=thread, command_id=uuid4(), body=NOTICE
        ).message_id
        notice = ReleaseNoticeTemplate.objects.get(clinic_id=w.clinic)
    return {
        "organization_id": graph.organization_a,
        "clinic_id": w.clinic,
        "release_id": release_row.pk,
        "version_id": version,
        "enrollment_id": chart.enrollment,
        "audience": "patient",
        "template_id": notice.pk,
        "message_id": message,
        "released_by_id": graph.physician,
    }


def _raw_delivery(fields: dict[str, object]) -> ReleaseDelivery:
    return ReleaseDelivery.objects.create(**fields)


def coordination_delivery_read(w: World) -> None:
    fields = _delivery_fields(w)
    with w.acting(w.graph.physician):
        delivery = _raw_delivery(fields)
    with w.acting(w.receptionist):
        assert care_visible(ReleaseDelivery) == set()
    with w.acting(w.graph.physician):
        assert care_visible(ReleaseDelivery) == {delivery.pk}


def coordination_delivery_insert(w: World) -> None:
    fields = _delivery_fields(w)
    with w.acting(w.receptionist):
        forged = {**fields, "released_by_id": w.receptionist}
        refused(_refuses("delivery refused"), lambda: _raw_delivery(forged))
    with w.acting(w.graph.physician):
        assert _raw_delivery(fields).released_by_id == w.graph.physician


def delivery_by_another(w: World) -> None:
    fields = _delivery_fields(w)
    other = _clinician(w)
    with owner_context(w.graph.organization_a):
        CareTeamMembership.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            patient_enrollment_id=cast("UUID", fields["enrollment_id"]),
            user_id=other,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
    with w.acting(other):
        refused(_refuses("delivery refused"), lambda: _raw_delivery(fields))


def delivery_without_release(w: World) -> None:
    fields = _delivery_fields(w)
    narrow(w, "physician", "result.release")
    with w.acting(w.graph.physician):
        refused(_refuses("delivery refused"), lambda: _raw_delivery(fields))


def _concern(w: World) -> UrgentConcern:
    """One open concern under a nurse-responsible coverage policy."""
    patient_reply(urgent_world(w.graph, roles=("nurse",)), URGENT)
    with owner_context(w.graph.organization_a):
        return UrgentConcern.objects.get()


def _decide(concern: UrgentConcern, **fields: object) -> Callable[[], int]:
    return lambda: UrgentConcern.objects.filter(pk=concern.pk).update(
        revision=F("revision") + 1, **fields
    )


def coordination_concern_read(w: World) -> None:
    concern = _concern(w)
    with w.acting(w.receptionist):
        assert care_visible(UrgentConcern) == set()
    with w.acting(w.nurse):
        assert care_visible(UrgentConcern) == {concern.pk}


def coordination_concern_update(w: World) -> None:
    concern = _concern(w)

    def touch() -> int:
        return UrgentConcern.objects.filter(pk=concern.pk).update(
            revision=F("revision")
        )

    with w.acting(w.receptionist):
        assert touch() == 0
    with w.acting(w.nurse):
        refused(_refuses("concern transition refused"), touch)


def coordination_responsible(w: World) -> None:
    def responsible(user: UUID) -> object:
        with w.acting(user), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.workflows_urgent_responsible(%s,%s::varchar[])",
                [w.clinic, ["nurse"]],
            )
            row = cursor.fetchone()
        assert row is not None
        return row[0]

    assert responsible(w.nurse) is True
    assert responsible(w.receptionist) is False


def coordination_concern_transition(w: World) -> None:
    concern = _concern(w)
    with w.acting(w.nurse):
        refused(
            _refuses("concern transition refused"),
            _decide(concern, state="acknowledged", acknowledged_by_id=w.receptionist),
        )
        assert _decide(concern, state="acknowledged", acknowledged_by_id=w.nurse)() == 1


def concern_by_unresponsible(w: World) -> None:
    concern = _concern(w)
    with w.acting(w.manager):
        refused(
            _refuses("concern transition refused"),
            _decide(concern, state="acknowledged", acknowledged_by_id=w.manager),
        )


def concern_resolved_by_another(w: World) -> None:
    concern = _concern(w)
    with w.acting(w.nurse):
        _decide(concern, state="acknowledged", acknowledged_by_id=w.nurse)()
        refused(
            _refuses("concern transition refused"),
            _decide(concern, state="resolved", resolved_by_id=w.receptionist),
        )


@contextmanager
def _patient_message(w: World) -> Iterator[tuple[UrgentWorld, UUID]]:
    patient = urgent_world(w.graph)
    with runtime_role(), patient_session_context(patient.session):
        message = reply_to_portal_conversation(
            clinic_id=w.clinic,
            conversation_id=patient.conversation,
            body=ROUTINE,
            idempotency_key=uuid4(),
        ).message_id
        yield patient, message


@contextmanager
def _mixed_patient_actor(w: World) -> Iterator[None]:
    # A savepoint restores the GUC even when the authority probe refuses.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [str(w.manager)]
            )
        yield
        transaction.set_rollback(True)


def _report_message(message: UUID) -> UUID | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.workflows_report_urgent(%s)", [message])
        row = cursor.fetchone()
    assert row is not None
    return cast("UUID | None", row[0])


def coordination_patient_coverage(w: World) -> None:
    with _patient_message(w), connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.workflows_patient_coverage()")
        assert cursor.fetchall() == [
            ([0, 1, 2, 3, 4, 5, 6], time(0, 0), time(23, 59), 1)
        ]
        with _mixed_patient_actor(w):
            cursor.execute("SELECT * FROM clinic_app.workflows_patient_coverage()")
            assert cursor.fetchall() == []


def coordination_patient_report(w: World) -> None:
    with _patient_message(w) as (_patient, message):
        concern = _report_message(message)
        assert isinstance(concern, UUID)
        with _mixed_patient_actor(w):
            refused(
                ("42501", "patient access denied"), lambda: _report_message(message)
            )
        assert _report_message(message) is None
        refused(("42501", "patient access denied"), lambda: _report_message(uuid4()))
    with owner_context(w.graph.organization_a):
        assert list(
            UrgentConcern.objects.filter(message_id=message).values_list(
                "pk", flat=True
            )
        ) == [concern]


def coordination_patient_audit_scope(w: World) -> None:
    # The scope helper is private. Runtime reaches it only through the audit
    # writer, which owns the helper's grant and still checks patient authority.
    with _patient_message(w) as (_patient, message), connection.cursor() as cursor:
        concern = _report_message(message)
        assert isinstance(concern, UUID)
        query = "SELECT * FROM clinic_app.workflows_urgent_audit_scope(%s)"
        denial = (
            "42501",
            "permission denied for function workflows_urgent_audit_scope",
        )
        refused(denial, lambda: cursor.execute(query, [concern]))
        refused(denial, lambda: cursor.execute(query, [uuid4()]))
        assert _append_patient_audit(concern) > 0
        with _mixed_patient_actor(w):
            refused(denial, lambda: cursor.execute(query, [concern]))
            refused(
                ("42501", "patient access denied"),
                lambda: _append_patient_audit(concern),
            )


def _append_patient_audit(concern: UUID) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.workflows_urgent_audit(%s,clock_timestamp(),%s)",
            [concern, b"s" * 32],
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def coordination_patient_audit(w: World) -> None:
    with _patient_message(w) as (patient, message):
        concern = _report_message(message)
        assert isinstance(concern, UUID)

        sequence = _append_patient_audit(concern)
        with _mixed_patient_actor(w):
            refused(
                ("42501", "patient access denied"),
                lambda: _append_patient_audit(concern),
            )
    with owner_context(w.graph.organization_a):
        event = AuditEvent.objects.get(
            organization_id=w.graph.organization_a, seq=sequence
        )
    assert event.actor_user_id == patient.session
    assert event.payload == {
        "clinic_id": str(w.clinic),
        "object_verb": "reported",
    }


def coordination_delivery_protocol(w: World) -> None:
    """Exercise the real callback alone, including its wrong-tenant no-op."""
    fields = _delivery_fields(w)
    with w.acting(w.graph.physician):
        delivery = _raw_delivery(fields)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.workflows_release_send_eligible(%s)",
                [delivery.message_id],
            )
            assert cursor.fetchone() == (True,)
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TEMP TABLE coordination_outcome_probe (
                organization_id uuid, subject_type text, subject_id uuid,
                status text, last_error text
            );
            CREATE TRIGGER coordination_outcome_probe
            AFTER UPDATE OF status ON coordination_outcome_probe FOR EACH ROW
            WHEN (NEW.status IN ('failed','cancelled')
                AND OLD.status IS DISTINCT FROM NEW.status
                AND NEW.subject_type='comms.message')
            EXECUTE FUNCTION clinic_app.workflows_release_outcome();
            GRANT SELECT,UPDATE ON coordination_outcome_probe TO clinic_app;
        """)
        cursor.execute(
            "INSERT INTO coordination_outcome_probe VALUES "
            "(%s,'comms.message',%s,'pending','adapter_unavailable')",
            [w.graph.organization_a, delivery.message_id],
        )

    def transition(tenant: UUID, state: str) -> None:
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true), "
                "set_config('app.current_user_id','',true)",
                [str(tenant)],
            )
            cursor.execute(
                "UPDATE pg_temp.coordination_outcome_probe SET status=%s", [state]
            )

    try:
        transition(w.graph.organization_b, "failed")
        with owner_context(w.graph.organization_a):
            delivery.refresh_from_db()
            assert delivery.failure_reason == ""
            assert delivery.failure_task_id is None
        transition(w.graph.organization_a, "pending")
        transition(w.graph.organization_a, "failed")
        transition(w.graph.organization_a, "pending")
        transition(w.graph.organization_a, "failed")
        with owner_context(w.graph.organization_a):
            delivery.refresh_from_db()
            tasks = list(Task.objects.filter(idempotency_key=delivery.pk))
        assert delivery.failure_reason == "send_failed"
        assert [task.pk for task in tasks] == [delivery.failure_task_id]
        assert tasks[0].owner_role == "receptionist"
        assert tasks[0].created_by_id is None
        with w.acting(w.graph.physician), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.workflows_release_send_eligible(%s)",
                [delivery.message_id],
            )
            assert cursor.fetchone() == (False,)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE pg_temp.coordination_outcome_probe")


COORDINATION_CASES: dict[str, Callable[[World], None]] = {
    "policy:workflows_releasenoticetemplate.coordination_read": (
        coordination_notice_read
    ),
    "policy:workflows_releasedelivery.coordination_read": coordination_delivery_read,
    "policy:workflows_urgentcoveragepolicy.coordination_read": (
        coordination_coverage_read
    ),
    "policy:workflows_urgentconcern.coordination_read": coordination_concern_read,
    "policy:workflows_urgentconcern.coordination_update": coordination_concern_update,
    "function:workflows_urgent_responsible(uuid,character varying[])": (
        coordination_responsible
    ),
    "function:workflows_patient_coverage()": coordination_patient_coverage,
    "function:workflows_report_urgent(uuid)": coordination_patient_report,
    "function:workflows_urgent_audit_scope(uuid)": coordination_patient_audit_scope,
    "function:workflows_urgent_audit(uuid,timestamp with time zone,bytea)": (
        coordination_patient_audit
    ),
    "function:workflows_release_send_eligible(uuid)": coordination_delivery_protocol,
    "function:workflows_release_outcome()": coordination_delivery_protocol,
    COORDINATION + "notice refused": coordination_notice_insert,
    COORDINATION + "coverage refused": coordination_coverage_insert,
    COORDINATION + "delivery refused": coordination_delivery_insert,
    COORDINATION + "concern transition refused": coordination_concern_transition,
}
COORDINATION_CONDITIONS: dict[str, Callable[[World], None]] = {
    COORDINATION + "notice refused#1:actor:NEW.approved_by_id IS DISTINCT FROM actor": (
        notice_by_another
    ),
    COORDINATION + "notice refused#2:permission:configuration.propose": (
        notice_without_release
    ),
    COORDINATION
    + "coverage refused#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        coverage_by_another
    ),
    COORDINATION + "coverage refused#2:permission:configuration.clinic": (
        coverage_without_configuration
    ),
    COORDINATION
    + "delivery refused#1:actor:NEW.released_by_id IS DISTINCT FROM actor": (
        delivery_by_another
    ),
    COORDINATION + "delivery refused#2:permission:result.release": (
        delivery_without_release
    ),
    COORDINATION + "concern transition refused#1:helper:workflows_urgent_responsible": (
        concern_by_unresponsible
    ),
    COORDINATION
    + (
        "concern transition refused#2:actor:"
        "NEW.acknowledged_by_id IS NOT DISTINCT FROM actor"
    ): coordination_concern_transition,
    COORDINATION
    + (
        "concern transition refused#3:actor:"
        "NEW.resolved_by_id IS NOT DISTINCT FROM actor"
    ): concern_resolved_by_another,
}


GUARD = "branch:workflows_guard():"
CASES: dict[str, Callable[[World], None]] = {
    "policy:workflows_task.workflow_read": task_read,
    "policy:workflows_task.workflow_update": task_update,
    "policy:workflows_taskcomment.workflow_read": comment_read,
    "policy:workflows_workflowdefinitionversion.workflow_read": definition_read,
    "policy:workflows_workflowdefinitionversion.workflow_update": definition_update,
    "policy:workflows_workflowrun.workflow_read": run_read,
    "policy:workflows_workflowrun.workflow_update": run_update,
    "policy:workflows_workflowstep.workflow_read": step_read,
    "policy:workflows_workflowstep.workflow_update": step_update,
    "function:workflows_owned(uuid,uuid,text)": owned,
    "function:workflows_owner_valid(uuid,uuid,text)": owner_valid,
    "function:workflows_staff_catalog(uuid)": staff_catalog,
    "function:workflows_predecessor_ready(uuid,uuid)": predecessor_ready,
    GUARD + "invalid task": invalid_task,
    GUARD + "task assignment denied": assignment,
    GUARD + "task completion denied": completion,
    GUARD + "task cancellation denied": cancellation,
    GUARD + "invalid escalation": escalation,
    GUARD + "comment denied": comment_trigger,
    GUARD + "definition denied": definition_authority,
    GUARD + "definition denied#2": definition_trigger,
    GUARD + "publication changed content": publication_actor,
    GUARD + "run retry denied": run_retry_actor,
    GUARD + "run denied": run_trigger,
    GUARD + "run transition denied": run_transition,
    GUARD + "step authority denied": step_authority,
    **CARE_CASES,
    **COORDINATION_CASES,
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
            lambda: WorkflowRun.objects.filter(pk=w.run.pk).update(
                state="running", revision=F("revision") + 1
            ),
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
    "definition denied#2#1:actor:NEW.created_by_id IS DISTINCT FROM actor": (
        definition_trigger
    ),
    "definition denied#1:permission:tasks.reassign": _manager_without(
        "tasks.reassign", raw_definition, "definition denied"
    ),
    "definition denied#2:permission:tasks.view": _manager_without(
        "tasks.view", raw_definition, "definition denied"
    ),
    "publication changed content#1:actor:NEW.published_by_id IS DISTINCT FROM actor": (
        publication_actor
    ),
    (
        "run retry denied#1:actor:OLD.started_by_id IS DISTINCT FROM actor"
    ): run_retry_actor,
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
    **CARE_CONDITIONS,
    **COORDINATION_CONDITIONS,
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


@pytest.mark.parametrize(
    "statement",
    [
        "PERFORM current_setting('app.current_user_id', true)",
        "PERFORM clinic_app.unclassified_workflow_helper()",
    ],
)
def test_foreign_trigger_actor_or_opaque_closure_fails_the_inventory(
    statement: str,
) -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE FUNCTION clinic_app.planted_workflow_trigger() "
                "RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN "
                + statement
                + "; RETURN NEW; END $f$"
            )
            cursor.execute(
                "CREATE TRIGGER planted_workflow_trigger BEFORE UPDATE "
                "ON clinic_app.workflows_workflowdefinitionversion "
                "FOR EACH ROW EXECUTE FUNCTION clinic_app.planted_workflow_trigger()"
            )
        with pytest.raises(AssertionError, match="foreign trigger function"):
            discover_sql_sites()
        transaction.set_rollback(True)
    assert set(discover_sql_sites()) == set(CASES)


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
