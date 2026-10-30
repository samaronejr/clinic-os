"""Seeded guard cases for the care package (todo 52).

Each case runs against `care_world.seed_care_world`: the guard actor is the plan
clinician and creator of every row, so a non-permitted role is refused by its
permission and never by row visibility. Replay cells make the four request
fingerprint relations and the command-digest relation decide both ways. Sites
reached through the shared task services are declared `bound` where a care
entrypoint fixes them by construction.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from apps.workflows.care import access, plans, pro, recalls, referrals
from apps.workflows.care import tasks as care_tasks
from apps.workflows.care.models import CareAction, CarePlan, PROOccurrence, Referral
from apps.workflows.care.validation import CareActionSpec, PROSpec, validate_action
from apps.workflows.models import Task
from apps.workflows.validation import TaskOwner
from django.db import connection
from django.db.models import Model
from django.utils import timezone
from psycopg import sql

from identity.permission_support import permission_context
from workflows.guard_cases import (
    ALLOWED,
    CONFLICT,
    DENIED,
    MANAGER,
    UNAUTHORIZED,
    Cell,
    GuardCase,
    GuardWorld,
    acting,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

PHYSICIAN = "physician"
PKG = "apps.workflows.care."
CALM = {f"item_{index}": "0" for index in range(1, 10)}
INVISIBLE = {"tasks.view": (DENIED, None)}


# The care services fix this create_task relation by construction: an action's
# task key derives from its own id, so the stored fingerprint always matches.
TASK_FINGERPRINT = (
    "apps.workflows.task_services.create_task:relation:task.fingerprint != fingerprint"
)


@contextmanager
def _owner_read() -> Iterator[None]:
    """Read case inputs as the table owner, whatever the acting role may see.

    A revoked, inactive or narrowed actor must meet the guard's refusal, not an
    error from loading the case's own fixture row through RLS first.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
        assert row is not None
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(str(row[0]))))


def _fresh[M: Model](model: type[M], pk: UUID) -> M:
    with _owner_read():
        return model._default_manager.get(pk=pk)


def _rev(model: type[CareAction | Referral | PROOccurrence], pk: UUID) -> int:
    with _owner_read():
        revision = model.objects.filter(pk=pk).values_list("revision", flat=True)
        return int(revision.get())


def _spec(kind: str = "staff_coordination", **extra: object) -> CareActionSpec:
    now = timezone.now()
    return CareActionSpec(
        kind=kind,
        window_start=now,
        window_end=now + timedelta(days=7),
        **extra,  # type: ignore[arg-type]
    )


def _pro_spec() -> PROSpec:
    return PROSpec(
        instrument_key="phq9",
        first_due_at=timezone.now() + timedelta(days=7),
        interval_days=7,
        occurrences=2,
        response_role="physician",
        staffing_policy_ref="eg3-pending-staffing-policy",
    )


def _replay(
    call: Callable[[GuardWorld, UUID, Any], object],
    make: Callable[[], Any],
    other: Callable[[Any], Any],
) -> tuple[Cell, Cell]:
    """The same request again is a no-op; the same key with new terms conflicts."""

    def setup(w: GuardWorld) -> tuple[UUID, Any]:
        terms, key = make(), uuid4()
        call(w, key, terms)
        return key, terms

    return (
        Cell(
            "replay-of-the-same-request",
            lambda w, p: call(w, p[0], p[1]),
            ALLOWED,
            None,
            setup,
        ),
        Cell(
            "replay-with-changed-terms",
            lambda w, p: call(w, p[0], other(p[1])),
            CONFLICT,
            None,
            setup,
        ),
    )


def _assign(w: GuardWorld, user: UUID, revision: int) -> object:
    return plans.assign_care_action(
        clinic_id=w.clinic,
        action_id=w.care.window,
        owner=TaskOwner(user_id=user),
        expected_revision=revision,
    )


def _claim_colleague(w: GuardWorld) -> object:
    """Claim the open action a colleague manager created (its task is theirs)."""
    return plans.assign_care_action(
        clinic_id=w.clinic,
        action_id=w.care.colleague,
        owner=TaskOwner(user_id=w.actor),
        expected_revision=_rev(CareAction, w.care.colleague),
    )


def _colleague_task_for(
    owner: Callable[[GuardWorld], UUID],
) -> Callable[[GuardWorld], int]:
    """The colleague manager gives their action's task to ``owner``."""

    def setup(w: GuardWorld) -> int:
        with acting(w.other):
            plans.assign_care_action(
                clinic_id=w.clinic,
                action_id=w.care.colleague,
                owner=TaskOwner(user_id=owner(w)),
                expected_revision=_rev(CareAction, w.care.colleague),
            )
        return _rev(CareAction, w.care.colleague)

    return setup


def _assign_replays() -> tuple[Cell, ...]:
    def setup(w: GuardWorld) -> int:
        revision = _rev(CareAction, w.care.window)
        _assign(w, w.actor, revision)
        return revision

    return (
        Cell(
            "replay-of-the-same-assignment",
            lambda w, revision: _assign(w, w.actor, revision),
            ALLOWED,
            None,
            setup,
        ),
        Cell(
            "replay-with-another-owner",
            lambda w, revision: _assign(w, w.other, revision),
            CONFLICT,
            None,
            setup,
        ),
        # Self-claim relations decide both ways, as in the task workspace: giving
        # one's task away or claiming a colleague's needs a manager, and a
        # colleague's task is invisible to other staff.
        Cell(
            "give-own-window-to-a-colleague",
            lambda w, _p: _assign(w, w.other, _rev(CareAction, w.care.window)),
            UNAUTHORIZED,
        ),
        Cell("claim-a-colleague-task", lambda w, _p: _claim_colleague(w), DENIED),
        Cell(
            "manager-claims-a-colleague-task",
            lambda w, _p: _claim_colleague(w),
            ALLOWED,
            MANAGER,
        ),
        # Work the actor owns is theirs to take, whoever created it.
        Cell(
            "reclaim-own-task-a-colleague-created",
            lambda w, revision: plans.assign_care_action(
                clinic_id=w.clinic,
                action_id=w.care.colleague,
                owner=TaskOwner(user_id=w.actor),
                expected_revision=revision,
            ),
            ALLOWED,
            None,
            _colleague_task_for(lambda w: w.actor),
        ),
    )


def _taskless(
    call: Callable[[GuardWorld, int], object],
) -> tuple[Cell, Cell]:
    """A patient instruction has no staff task: only the clinician acts on it."""

    def run(w: GuardWorld, _p: object) -> object:
        return call(w, _rev(CareAction, w.care.note))

    return (
        Cell("task-less-instruction-by-the-clinician", run, ALLOWED, PHYSICIAN),
        # Invisible to staff (RP matrix): the unknown-record denial.
        Cell("task-less-instruction-by-staff", run, DENIED, MANAGER),
    )


def _fresh_overdue(w: GuardWorld) -> GuardWorld:
    """Each transport cell runs against its own newly due overdue action."""
    with permission_context(w.graph, w.actor):
        action = plans.add_care_action(
            clinic_id=w.clinic,
            plan_id=w.care.plan,
            spec=replace(
                _spec(),
                window_start=timezone.now() - timedelta(days=3),
                window_end=timezone.now() - timedelta(days=1),
            ),
            idempotency_key=uuid4(),
        )
    return replace(w, care=replace(w.care, overdue=action.pk))


def _cancel_window(w: GuardWorld) -> object:
    action = _fresh(CareAction, w.care.window)
    assert action.task_id is not None
    return plans._cancel(action, _fresh(Task, action.task_id))


def care_cases() -> tuple[GuardCase, ...]:
    add_cells = _replay(
        lambda w, key, spec: plans.add_care_action(
            clinic_id=w.clinic, plan_id=w.care.plan, spec=spec, idempotency_key=key
        ),
        _spec,
        lambda spec: replace(spec, window_end=spec.window_end + timedelta(days=1)),
    )
    create_cells = _replay(
        lambda w, key, actions: plans.create_care_plan_from_version(
            clinic_id=w.clinic,
            version_id=w.care.new_version,
            actions=actions,
            idempotency_key=key,
        ),
        lambda: (_spec("return_window"),),
        lambda actions: (
            replace(actions[0], window_end=actions[0].window_end + timedelta(days=2)),
        ),
    )
    pro_cells = _replay(
        lambda w, key, spec: pro.assign_pro(
            clinic_id=w.clinic,
            enrollment_id=w.enrollment,
            spec=spec,
            idempotency_key=key,
        ),
        _pro_spec,
        lambda spec: replace(spec, interval_days=14),
    )
    recall_cells = _replay(
        lambda w, key, days: recalls.schedule_recall(
            clinic_id=w.clinic,
            enrollment_id=w.enrollment,
            reason="routine_return",
            interval_days=days,
            owner_role="receptionist",
            idempotency_key=key,
        ),
        lambda: 30,
        lambda days: days + 30,
    )
    return (
        GuardCase(
            PKG + "access.require_staff",
            "tasks.assign",
            lambda w: access.require_staff(clinic_id=w.clinic),
        ),
        GuardCase(
            PKG + "access.require_clinician",
            "clinical.write",
            lambda w: access.require_clinician(
                clinic_id=w.clinic, enrollment_id=w.enrollment
            ),
        ),
        GuardCase(
            PKG + "access.require_observer",
            "clinical.write",
            lambda w: access.require_observer(
                clinic_id=w.clinic, enrollment_id=w.enrollment
            ),
        ),
        GuardCase(
            PKG + "access.load_plan",
            "tasks.view",
            lambda w: access.load_plan(clinic_id=w.clinic, plan_id=w.care.plan),
        ),
        GuardCase(
            PKG + "access.load_action",
            "tasks.assign",
            lambda w: access.load_action(
                clinic_id=w.clinic, action_id=w.care.window, permission="tasks.assign"
            ),
        ),
        GuardCase(
            PKG + "plans._create_action",
            "tasks.assign",
            lambda w: plans._create_action(
                plan=_fresh(CarePlan, w.care.plan),
                spec=_spec("return_window"),
                terms=validate_action(_spec("return_window")),
                actor=w.actor,
                key=uuid4(),
            ),
            bound=frozenset({TASK_FINGERPRINT}),
        ),
        GuardCase(
            PKG + "plans._cancel",
            "tasks.reassign",
            _cancel_window,
        ),
        GuardCase(
            PKG + "plans.create_care_plan_from_version",
            "clinical.write",
            lambda w: plans.create_care_plan_from_version(
                clinic_id=w.clinic,
                version_id=w.care.new_version,
                actions=(_spec("return_window"),),
                idempotency_key=uuid4(),
            ),
            cells=create_cells,
            bound=frozenset({TASK_FINGERPRINT}),
        ),
        GuardCase(
            PKG + "plans.add_care_action",
            "tasks.assign",
            lambda w: plans.add_care_action(
                clinic_id=w.clinic,
                plan_id=w.care.plan,
                spec=_spec(),
                idempotency_key=uuid4(),
            ),
            cells=(
                *add_cells,
                Cell(
                    "clinical-kind-by-the-clinician",
                    lambda w, _p: plans.add_care_action(
                        clinic_id=w.clinic,
                        plan_id=w.care.plan,
                        spec=_spec("patient_instruction", instruction="Repouso."),
                        idempotency_key=uuid4(),
                    ),
                    ALLOWED,
                    PHYSICIAN,
                ),
                Cell(
                    "clinical-kind-by-staff",
                    lambda w, _p: plans.add_care_action(
                        clinic_id=w.clinic,
                        plan_id=w.care.plan,
                        spec=_spec("patient_instruction", instruction="Repouso."),
                        idempotency_key=uuid4(),
                    ),
                    UNAUTHORIZED,
                    MANAGER,
                ),
            ),
            bound=frozenset({TASK_FINGERPRINT}),
        ),
        GuardCase(
            PKG + "plans.assign_care_action",
            "tasks.assign",
            lambda w: _assign(w, w.actor, _rev(CareAction, w.care.window)),
            cells=_assign_replays(),
            narrowed=INVISIBLE,
        ),
        GuardCase(
            PKG + "plans.complete_care_action",
            "tasks.complete",
            lambda w: plans.complete_care_action(
                clinic_id=w.clinic,
                action_id=w.care.assigned,
                outcome="resolved",
                expected_revision=_rev(CareAction, w.care.assigned),
            ),
            cells=(
                *_taskless(
                    lambda w, revision: plans.complete_care_action(
                        clinic_id=w.clinic,
                        action_id=w.care.note,
                        outcome="resolved",
                        expected_revision=revision,
                    )
                ),
                # Completion is the owner's, as in the task workspace: a manager
                # sees a colleague's task but cannot complete it.
                Cell(
                    "owned-by-a-colleague-as-a-manager",
                    lambda w, revision: plans.complete_care_action(
                        clinic_id=w.clinic,
                        action_id=w.care.colleague,
                        outcome="resolved",
                        expected_revision=revision,
                    ),
                    DENIED,
                    MANAGER,
                    _colleague_task_for(lambda w: w.other),
                ),
            ),
            narrowed=INVISIBLE,
        ),
        GuardCase(
            PKG + "plans.cancel_care_action",
            "tasks.reassign",
            lambda w: plans.cancel_care_action(
                clinic_id=w.clinic,
                action_id=w.care.window,
                expected_revision=_rev(CareAction, w.care.window),
            ),
            cells=_taskless(
                lambda w, revision: plans.cancel_care_action(
                    clinic_id=w.clinic,
                    action_id=w.care.note,
                    expected_revision=revision,
                )
            ),
        ),
        GuardCase(
            PKG + "plans.review_care_action",
            "clinical.write",
            lambda w: plans.review_care_action(
                clinic_id=w.clinic,
                action_id=w.care.flagged,
                decision="confirm",
                expected_revision=_rev(CareAction, w.care.flagged),
            ),
        ),
        GuardCase(
            PKG + "plans.flag_stale_care_plan",
            "tasks.assign",
            lambda w: plans.flag_stale_care_plan(
                clinic_id=w.clinic, plan_id=w.care.stale_plan
            ),
        ),
        GuardCase(
            PKG + "plans.mark_care_action_overdue",
            "tasks.assign",
            lambda w: plans.mark_care_action_overdue(
                clinic_id=w.clinic, action_id=w.care.overdue
            ),
        ),
        GuardCase(
            PKG + "referrals.link_referral_document",
            "clinical.write",
            lambda w: referrals.link_referral_document(
                clinic_id=w.clinic,
                referral_id=w.care.referral,
                draft_id=w.care.referral_draft,
                expected_revision=_rev(Referral, w.care.referral),
            ),
            role_denial=DENIED,
        ),
        GuardCase(
            PKG + "referrals.advance_referral",
            "tasks.assign",
            lambda w: referrals.advance_referral(
                clinic_id=w.clinic,
                referral_id=w.care.referral,
                status="scheduled",
                expected_revision=_rev(Referral, w.care.referral),
            ),
            control_role="physician",
        ),
        GuardCase(
            PKG + "recalls.schedule_recall",
            "tasks.reassign",
            lambda w: recalls.schedule_recall(
                clinic_id=w.clinic,
                enrollment_id=w.enrollment,
                reason="routine_return",
                interval_days=30,
                owner_role="receptionist",
                idempotency_key=uuid4(),
            ),
            cells=recall_cells,
        ),
        GuardCase(
            PKG + "recalls.materialize_recall",
            "tasks.reassign",
            lambda w: recalls.materialize_recall(
                clinic_id=w.clinic, recall_id=w.care.recall
            ),
            narrowed=INVISIBLE,
            bound=frozenset({TASK_FINGERPRINT}),
        ),
        GuardCase(
            PKG + "pro.assign_pro",
            "clinical.write",
            lambda w: pro.assign_pro(
                clinic_id=w.clinic,
                enrollment_id=w.enrollment,
                spec=_pro_spec(),
                idempotency_key=uuid4(),
            ),
            cells=pro_cells,
        ),
        GuardCase(
            PKG + "pro.close_missed_pro_occurrence",
            "tasks.assign",
            lambda w: pro.close_missed_pro_occurrence(
                clinic_id=w.clinic, occurrence_id=w.care.occurrence
            ),
            control_role="physician",
        ),
        GuardCase(
            PKG + "pro.record_pro_response",
            "clinical.write",
            lambda w: pro.record_pro_response(
                clinic_id=w.clinic,
                occurrence_id=w.care.occurrence,
                answers=CALM,
                expected_revision=_rev(PROOccurrence, w.care.occurrence),
            ),
        ),
        GuardCase(
            PKG + "tasks.process_due_care",
            "tasks.assign",
            lambda w: care_tasks.process_due_care(
                clinic_id=w.clinic, kind="overdue", record_id=w.care.overdue
            ),
        ),
        GuardCase(
            PKG + "tasks.run_care",
            "tasks.assign",
            lambda w: care_tasks.run_care(
                kind="overdue", record_id=str(w.care.overdue)
            ),
            owns_transaction=True,
            transport=True,
            refresh=_fresh_overdue,
        ),
    )
