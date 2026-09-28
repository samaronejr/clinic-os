"""Atomic task commands; owner and revision are checked before every effect."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid5

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id
from apps.workflows.access import (
    may_self_claim,
    require_clinic_access,
    require_manager,
    require_owner_target,
    require_reference,
    require_task_access,
)
from apps.workflows.models import Task, TaskComment
from apps.workflows.validation import (
    KINDS,
    PRIORITIES,
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    digest,
    validate_owner,
    validate_task,
)
from apps.workflows.validation import (
    evidence as validated_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

MAX_COMMENT_LENGTH = 2000
TERMINAL = frozenset({"done", "cancelled"})
REPLAY_KEY_CONSTRAINT = "workflows_task_command_uniq"
COMMENT_KEY_CONSTRAINT = "workflows_taskcomment_pkey"


def _constraint(error: IntegrityError) -> object:
    return getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)


def _event(task: Task, verb: str) -> None:
    record_phase1_event(
        f"workflows.task.{verb}", clinic_id=task.clinic_id, affected_record_id=task.pk
    )


def _command(task: Task, expected: int, verb: str, terms: object) -> bool:
    """Derive one command key from row, verb and CAS revision; bind its payload."""
    key = uuid5(task.pk, f"{verb}:{expected}")
    fingerprint = digest({"actor": str(current_actor_id()), "terms": terms})
    if task.last_command_key == key:
        if task.last_command_digest != fingerprint:
            raise WorkflowConflictError
        return False
    if type(expected) is not int or task.revision != expected:
        raise WorkflowConflictError
    task.last_command_key, task.last_command_digest = key, fingerprint
    return True


def _dependency(task: Task) -> None:
    if (
        task.depends_on_id is not None
        # The composite (organization, clinic, depends_on) key already pins
        # the predecessor to this task's clinic.
        and not Task.objects.filter(pk=task.depends_on_id, state="done").exists()
    ):
        raise WorkflowConflictError


def _save(task: Task, fields: tuple[str, ...], verb: str) -> Task:
    task.revision += 1
    task.save(
        update_fields=(
            *fields,
            "revision",
            "last_command_key",
            "last_command_digest",
            "updated_at",
        )
    )
    _event(task, verb)
    return task


@transaction.atomic
def create_task(*, clinic_id: UUID, spec: TaskSpec, idempotency_key: UUID) -> Task:
    """Create once; the key binds the complete reference-only creation request."""
    clinic, actor = require_clinic_access(
        clinic_id=clinic_id, permission="tasks.assign"
    )
    terms = validate_task(spec)
    subject = require_reference(clinic_id=clinic_id, value=spec.subject_ref)
    if spec.depends_on is not None:
        require_task_access(
            clinic_id=clinic_id, task_id=spec.depends_on, permission="tasks.view"
        )
    fingerprint = digest({"actor": str(actor), **terms})
    try:
        task, created = Task.objects.get_or_create(
            clinic_id=clinic_id,
            idempotency_key=idempotency_key,
            defaults={
                "organization_id": clinic.organization_id,
                "created_by_id": actor,
                "kind": spec.kind,
                "subject_ref": subject,
                "due_at": spec.due_at,
                "priority": spec.priority,
                "depends_on_id": spec.depends_on,
                "escalation_policy_version": spec.escalation_policy_version,
                "fingerprint": fingerprint,
            },
        )
    except IntegrityError as error:
        # Only the replay key existing outside this actor's visibility is a
        # conflict; every other rejection stays a visible fault.
        if _constraint(error) != REPLAY_KEY_CONSTRAINT:
            raise
        raise WorkflowConflictError from error
    if task.fingerprint != fingerprint:
        raise WorkflowConflictError
    if created:
        _event(task, "created")
    return task


@transaction.atomic
def assign_task(
    *, clinic_id: UUID, task_id: UUID, owner: TaskOwner, expected_revision: int
) -> Task:
    """Claim for self, or transfer under exact-clinic manager authority."""
    task = require_task_access(
        clinic_id=clinic_id, task_id=task_id, permission="tasks.assign"
    )
    validate_owner(owner)
    require_owner_target(clinic_id=clinic_id, user_id=owner.user_id, role=owner.role)
    # Role owners (workflow steps included) are always a manager's assignment.
    if owner.user_id is None or not may_self_claim(task, owner):
        require_manager(clinic_id=clinic_id)
    if not _command(
        task,
        expected_revision,
        "assign",
        {"user": str(owner.user_id) if owner.user_id else None, "role": owner.role},
    ):
        return task
    if task.state in TERMINAL:
        raise WorkflowConflictError
    task.owner_user_id = owner.user_id
    task.owner_role = owner.role
    if task.state == "open":
        task.state = "assigned"
    return _save(task, ("owner_user_id", "owner_role", "state"), "assigned")


@transaction.atomic
def start_task(*, clinic_id: UUID, task_id: UUID, expected_revision: int) -> Task:
    """Begin assigned work only after its predecessor is done."""
    task = require_task_access(
        clinic_id=clinic_id, task_id=task_id, permission="tasks.complete", owned=True
    )
    if not _command(task, expected_revision, "start", {}):
        return task
    _dependency(task)
    if task.state != "assigned":
        raise WorkflowConflictError
    task.state = "in_progress"
    return _save(task, ("state",), "started")


@transaction.atomic
def complete_task(
    *,
    clinic_id: UUID,
    task_id: UUID,
    evidence: Mapping[str, object],
    expected_revision: int,
) -> Task:
    """Complete as the current owner with kind-specific, reference-only evidence."""
    task = require_task_access(
        clinic_id=clinic_id, task_id=task_id, permission="tasks.complete", owned=True
    )
    checked = validated_evidence(task.kind, dict(evidence))
    if not _command(task, expected_revision, "complete", checked):
        return task
    _dependency(task)
    if task.state != "in_progress":
        raise WorkflowConflictError
    if "record" in checked:
        require_reference(
            clinic_id=clinic_id, value=cast("Mapping[str, object]", checked["record"])
        )
    task.completion_evidence = checked
    task.state = "done"
    return _save(task, ("completion_evidence", "state"), "completed")


@transaction.atomic
def cancel_task(*, clinic_id: UUID, task_id: UUID, expected_revision: int) -> Task:
    """Cancel, never delete; cancellation cannot erase completion evidence."""
    task = require_task_access(
        clinic_id=clinic_id, task_id=task_id, permission="tasks.reassign"
    )
    if not _command(task, expected_revision, "cancel", {}):
        return task
    if task.state in TERMINAL:
        raise WorkflowConflictError
    task.state = "cancelled"
    return _save(task, ("state",), "cancelled")


@transaction.atomic
def add_comment(
    *, clinic_id: UUID, task_id: UUID, body: str, idempotency_key: UUID
) -> TaskComment:
    """Append encrypted discussion once; no free text enters audit or outbox."""
    task = require_task_access(
        clinic_id=clinic_id, task_id=task_id, permission="tasks.view"
    )
    _clinic, actor = require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENT_LENGTH:
        raise WorkflowInputError
    # The key is global: a replay that names another task, author or body -
    # including one outside this actor's visibility - is a conflict.
    try:
        comment, created = TaskComment.objects.get_or_create(
            pk=idempotency_key,
            defaults={
                "organization_id": task.organization_id,
                "clinic_id": clinic_id,
                "task": task,
                "author_id": actor,
                "body": body,
            },
        )
    except IntegrityError as error:
        if _constraint(error) != COMMENT_KEY_CONSTRAINT:
            raise
        raise WorkflowConflictError from error
    if comment.task_id != task_id or comment.author_id != actor or comment.body != body:
        raise WorkflowConflictError
    if created:
        record_phase1_event(
            "workflows.comment.created",
            clinic_id=clinic_id,
            affected_record_id=comment.pk,
        )
    return comment


def list_tasks(
    *,
    clinic_id: UUID,
    state: str = "",
    priority: str = "",
    kind: str = "",
    exceptions: bool = False,
) -> tuple[Task, ...]:
    """Return a bounded owner/RLS-filtered queue; overdue rows stay visible."""
    require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
    if (
        state not in {"", "open", "assigned", "in_progress", "done", "cancelled"}
        or priority not in {"", *PRIORITIES}
        or kind not in {"", *KINDS}
    ):
        raise WorkflowInputError
    rows = Task.objects.filter(clinic_id=clinic_id)
    if state:
        rows = rows.filter(state=state)
    if priority:
        rows = rows.filter(priority=priority)
    if kind:
        rows = rows.filter(kind=kind)
    if exceptions:
        rows = rows.exclude(state__in=TERMINAL).filter(due_at__lte=timezone.now())
    return tuple(rows.order_by("due_at", "id")[:100])


@transaction.atomic
def escalate_task(*, clinic_id: UUID, task_id: UUID) -> bool:
    """Apply pinned escalation policy one once; never substitute a new policy."""
    task = require_task_access(
        clinic_id=clinic_id, task_id=task_id, permission="tasks.assign"
    )
    if (
        task.state in TERMINAL
        or task.escalated_at is not None
        or task.due_at > timezone.now()
    ):
        return False
    _command(task, task.revision, "escalate", {})
    task.escalated_at = timezone.now()
    _save(task, ("escalated_at",), "escalated")
    return True
