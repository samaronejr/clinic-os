"""Care plans from finalized note versions and their typed, owned actions.

A plan never carries clinical narrative: free text lives only in an encrypted
patient instruction. Clinical kinds need the clinician's own approval; staff
execute administrative kinds through the single ``Task`` entity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id
from apps.workflows import engine
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.access import (
    bind_command,
    keyed_digests,
    load_action,
    load_plan,
    plan_of,
    require_clinician,
    require_current_source,
    require_staff,
    save_command,
    version_scope,
)
from apps.workflows.care.models import CareAction, CarePlan, Referral
from apps.workflows.care.validation import (
    CareActionSpec,
    validate_action,
)
from apps.workflows.care.vocab import CLINICAL_KINDS, OUTCOMES, TASKLESS_KINDS
from apps.workflows.task_services import (
    assign_task,
    cancel_task,
    complete_task,
    create_task,
    start_task,
)
from apps.workflows.validation import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    digest,
    validate_owner,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from apps.workflows.models import Task

TERMINAL = frozenset({"done", "cancelled"})
MAX_ACTIONS = 16


def _fingerprints(
    actor: UUID, plan_id: UUID, terms: dict[str, object]
) -> tuple[str, ...]:
    return keyed_digests({"actor": str(actor), "plan": str(plan_id), **terms})


def _create_action(
    *,
    plan: CarePlan,
    spec: CareActionSpec,
    terms: dict[str, object],
    actor: UUID,
    key: UUID,
) -> CareAction:
    """Insert one action; its task (if any) is created first and bound by id."""
    # Keys are unique per clinic, so the id derived from one names its clinic:
    # another clinic's (or tenant's) key never collides with this action.
    action_id = uuid5(key, f"care-action:{plan.clinic_id}")
    task_id = None
    if spec.kind not in TASKLESS_KINDS:
        task_id = create_task(
            clinic_id=plan.clinic_id,
            spec=TaskSpec(
                kind="follow_up",
                subject_ref={"kind": "enrollment", "id": str(plan.enrollment_id)},
                due_at=spec.window_end,
            ),
            idempotency_key=action_id,
        ).pk
    action = CareAction.objects.create(
        id=action_id,
        organization_id=plan.organization_id,
        clinic_id=plan.clinic_id,
        plan=plan,
        kind=spec.kind,
        window_start=spec.window_start,
        window_end=spec.window_end,
        origin_version_id=plan.source_version_id,
        approved_by_id=actor if spec.kind in CLINICAL_KINDS else None,
        instruction=terms.get("instruction"),
        task_id=task_id,
        created_by_id=actor,
        idempotency_key=key,
        fingerprint=_fingerprints(actor, plan.pk, terms)[-1],
    )
    record_phase1_event(
        "workflows.care_action.created",
        clinic_id=plan.clinic_id,
        affected_record_id=action.pk,
    )
    if spec.kind == "referral":
        target = terms["referral"]
        assert isinstance(target, dict)  # noqa: S101 - validated by validate_action
        referral = Referral.objects.create(
            organization_id=plan.organization_id,
            clinic_id=plan.clinic_id,
            plan=plan,
            action=action,
            target_kind=target["target_kind"],
            target_label=target["target_label"],
            created_by_id=actor,
        )
        record_phase1_event(
            "workflows.referral.created",
            clinic_id=plan.clinic_id,
            affected_record_id=referral.pk,
        )
    return action


@transaction.atomic
def create_care_plan_from_version(
    *,
    clinic_id: UUID,
    version_id: UUID,
    actions: Sequence[CareActionSpec],
    idempotency_key: UUID,
) -> CarePlan:
    """Turn a finalized note version into one plan of typed, owned actions."""
    if (
        not isinstance(idempotency_key, UUID)
        or not isinstance(actions, (tuple, list))
        or not 1 <= len(actions) <= MAX_ACTIONS
    ):
        raise WorkflowInputError
    terms = [validate_action(spec) for spec in actions]
    clinic = require_staff(clinic_id=clinic_id, permission="tasks.view")
    encounter_id, enrollment_id, state = version_scope(
        clinic_id=clinic_id, version_id=version_id
    )
    actor = require_clinician(clinic_id=clinic_id, enrollment_id=enrollment_id)
    # Staff actions are bound to tasks: decide that authority before any write,
    # so a refusal never leaves a plan row behind in the transaction.
    if any(spec.kind not in TASKLESS_KINDS for spec in actions):
        require_staff(clinic_id=clinic_id, permission="tasks.assign")
    if state != "finalized":
        raise WorkflowConflictError
    fingerprints = keyed_digests(
        {"actor": str(actor), "version": str(version_id), "actions": terms}
    )
    replay = CarePlan.objects.filter(
        clinic_id=clinic_id, idempotency_key=idempotency_key
    ).first()
    if replay is not None:
        if replay.fingerprint not in fingerprints:
            raise WorkflowConflictError
        return replay
    if CarePlan.objects.filter(source_version_id=version_id).exists():
        raise WorkflowConflictError
    plan = CarePlan.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        encounter_id=encounter_id,
        source_version_id=version_id,
        clinician_id=actor,
        idempotency_key=idempotency_key,
        fingerprint=fingerprints[-1],
    )
    record_phase1_event(
        "workflows.care_plan.created", clinic_id=clinic_id, affected_record_id=plan.pk
    )
    for index, (spec, term) in enumerate(zip(actions, terms, strict=True)):
        _create_action(
            plan=plan,
            spec=spec,
            terms=term,
            actor=actor,
            key=uuid5(idempotency_key, f"action:{index}"),
        )
    # An earlier plan of this patient that a newer version made stale is
    # flagged by the scanner; until then the source gate refuses its work.
    return plan


@transaction.atomic
def add_care_action(
    *,
    clinic_id: UUID,
    plan_id: UUID,
    spec: CareActionSpec,
    idempotency_key: UUID,
) -> CareAction:
    """Add one action: clinical kinds need the clinician, administrative kinds staff."""
    if not isinstance(idempotency_key, UUID):
        raise WorkflowInputError
    terms = validate_action(spec)
    plan = load_plan(clinic_id=clinic_id, plan_id=plan_id)
    if spec.kind in CLINICAL_KINDS:
        actor = require_clinician(clinic_id=clinic_id, enrollment_id=plan.enrollment_id)
    else:
        require_staff(clinic_id=clinic_id, permission="tasks.assign")
        actor = current_actor_id()
    replay = CareAction.objects.filter(
        clinic_id=clinic_id, idempotency_key=idempotency_key
    ).first()
    if replay is not None:
        if replay.plan_id != plan.pk or replay.fingerprint not in _fingerprints(
            actor, plan.pk, terms
        ):
            raise WorkflowConflictError
        return replay
    if plan.state != "active":
        raise WorkflowConflictError
    return _create_action(
        plan=plan, spec=spec, terms=terms, actor=actor, key=idempotency_key
    )


def _current(action: CareAction) -> CareAction:
    """Reload the action the task services just mirrored."""
    return CareAction.objects.get(pk=action.pk)


@transaction.atomic
def assign_care_action(
    *, clinic_id: UUID, action_id: UUID, owner: TaskOwner, expected_revision: int
) -> CareAction:
    """Assign through the action's task; the action follows, overdue stays overdue."""
    action, task = load_action(
        clinic_id=clinic_id, action_id=action_id, permission="tasks.assign"
    )
    validate_owner(owner)
    terms = {"user": str(owner.user_id) if owner.user_id else None, "role": owner.role}
    if not bind_command(action, expected_revision, "assign", terms):
        return action
    if task is None or action.state in TERMINAL:
        raise WorkflowConflictError
    # The task service refuses a flagged or stale action before any effect.
    assign_task(
        clinic_id=clinic_id,
        task_id=task.pk,
        owner=owner,
        expected_revision=task.revision,
    )
    return _current(action)


@transaction.atomic
def complete_care_action(
    *, clinic_id: UUID, action_id: UUID, outcome: str, expected_revision: int
) -> CareAction:
    """Complete as the task owner; a patient instruction needs the clinician."""
    if outcome not in OUTCOMES:
        raise WorkflowInputError
    action, task = load_action(
        clinic_id=clinic_id, action_id=action_id, permission="tasks.complete"
    )
    if not bind_command(action, expected_revision, "complete", {"outcome": outcome}):
        return action
    if action.state in TERMINAL:
        raise WorkflowConflictError
    if task is None:
        require_clinician(
            clinic_id=clinic_id, enrollment_id=plan_of(action).enrollment_id
        )
        if action.review_state == "required":
            raise WorkflowConflictError
        require_current_source(action)
        action.state = "done"
        save_command(action, ("state",), "workflows.care_action.completed")
        return action
    if action.state == "open":
        raise WorkflowConflictError
    if task.state == "assigned":
        task = start_task(
            clinic_id=clinic_id, task_id=task.pk, expected_revision=task.revision
        )
    complete_task(
        clinic_id=clinic_id,
        task_id=task.pk,
        evidence={"record": task.subject_ref, "outcome": outcome},
        expected_revision=task.revision,
    )
    return _current(action)


@transaction.atomic
def cancel_care_action(
    *, clinic_id: UUID, action_id: UUID, expected_revision: int
) -> CareAction:
    """Cancel, never delete; a staff task is cancelled by a manager."""
    action, task = load_action(
        clinic_id=clinic_id, action_id=action_id, permission="tasks.view"
    )
    if task is None:
        require_clinician(
            clinic_id=clinic_id, enrollment_id=plan_of(action).enrollment_id
        )
    if not bind_command(action, expected_revision, "cancel", {}):
        return action
    if action.state in TERMINAL:
        raise WorkflowConflictError
    return _cancel(action, task)


def _cancel(action: CareAction, task: Task | None) -> CareAction:
    """Cancel the staff task (manager authority) or the task-less instruction."""
    if task is not None:
        cancel_task(
            clinic_id=action.clinic_id,
            task_id=task.pk,
            expected_revision=task.revision,
        )
        return _current(action)
    action.state = "cancelled"
    save_command(action, ("state",), "workflows.care_action.cancelled")
    return action


@transaction.atomic
def review_care_action(
    *, clinic_id: UUID, action_id: UUID, decision: str, expected_revision: int
) -> CareAction:
    """Let the clinician keep or cancel an action flagged by a changed source."""
    if decision not in {"confirm", "cancel"}:
        raise WorkflowInputError
    action, task = load_action(
        clinic_id=clinic_id, action_id=action_id, permission="tasks.view"
    )
    require_clinician(clinic_id=clinic_id, enrollment_id=plan_of(action).enrollment_id)
    # A review cancel is the cancel command: the same key, from either service.
    verb = "review" if decision == "confirm" else "cancel"
    terms = {"decision": decision} if decision == "confirm" else {}
    if not bind_command(action, expected_revision, verb, terms):
        return action
    if action.review_state != "required" or action.state in TERMINAL:
        raise WorkflowConflictError
    if decision == "cancel":
        return _cancel(action, task)
    action.review_state = "confirmed"
    save_command(action, ("review_state",), "workflows.care_action.reviewed")
    return action


@transaction.atomic
def flag_stale_care_plan(*, clinic_id: UUID, plan_id: UUID) -> bool:
    """Supersede one plan whose source left 'finalized' and flag its pending work.

    Returns False when nothing was left to change; the scanner then records a
    refusal if the plan is still due (an invisible action, for example).
    """
    require_staff(clinic_id=clinic_id, permission="tasks.assign")
    plan = (
        CarePlan.objects.select_for_update()
        .filter(pk=plan_id, clinic_id=clinic_id)
        .first()
    )
    if plan is None:
        raise WorkflowAccessDeniedError
    _encounter, _enrollment, source = version_scope(
        clinic_id=clinic_id, version_id=plan.source_version_id
    )
    if source == "finalized":
        return False
    changed = False
    if plan.state == "active":
        plan.last_command_key = uuid5(plan.pk, f"supersede:{plan.revision}")
        plan.last_command_digest = digest({"plan": str(plan.pk), "stale": True})
        plan.state = "superseded"
        save_command(plan, ("state",), "workflows.care_plan.superseded")
        changed = True
    pending = CareAction.objects.select_for_update().filter(
        plan_id=plan.pk,
        review_state="none",
        state__in=("open", "assigned", "overdue"),
    )
    for action in pending:
        action.last_command_key = uuid5(action.pk, f"flag:{action.revision}")
        action.last_command_digest = digest({"action": str(action.pk), "flag": 1})
        action.review_state = "required"
        save_command(action, ("review_state",), "workflows.care_action.flagged")
        changed = True
    return changed


@transaction.atomic
def mark_care_action_overdue(*, clinic_id: UUID, action_id: UUID) -> bool:
    """Mark one open or assigned action past its window overdue; never executes it.

    A flagged action is not due: it waits for its clinician, so it is neither
    marked nor listed again until reviewed.
    """
    require_staff(clinic_id=clinic_id, permission="tasks.assign")
    action = (
        CareAction.objects.select_for_update()
        .filter(pk=action_id, clinic_id=clinic_id)
        .first()
    )
    if action is None:
        raise WorkflowAccessDeniedError
    if (
        action.state not in {"open", "assigned"}
        or action.review_state == "required"
        or action.window_end > engine.utc_now()
    ):
        return False
    action.last_command_key = uuid5(action.pk, f"overdue:{action.revision}")
    action.last_command_digest = digest({"action": str(action.pk), "overdue": 1})
    action.state = "overdue"
    save_command(action, ("state",), "workflows.care_action.overdue")
    return True
