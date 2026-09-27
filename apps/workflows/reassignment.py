"""Signed bulk previews bind the actor, target and every task revision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID

from django.core import signing
from django.db import transaction

from apps.workflows.access import (
    require_manager,
    require_owner_target,
    require_task_access,
)
from apps.workflows.task_services import assign_task
from apps.workflows.validation import (
    TaskOwner,
    WorkflowConflictError,
    WorkflowInputError,
    validate_owner,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from apps.workflows.models import Task

SALT = "clinic-workflow-reassignment-v1"
MAX_BULK_TASKS = 100
PREVIEW_MAX_AGE_SECONDS = 900
MAX_PREVIEW_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class ReassignmentPreview:
    """A read-only preview; only its signed token can authorize an apply attempt."""

    tasks: tuple[Task, ...]
    owner: TaskOwner
    token: str


@transaction.atomic
def preview_reassignment(
    *, clinic_id: UUID, task_ids: Sequence[UUID], owner: TaskOwner
) -> ReassignmentPreview:
    """Read the exact selection without modifying tasks or emitting audit."""
    actor = require_manager(clinic_id=clinic_id)
    validate_owner(owner)
    require_owner_target(clinic_id=clinic_id, user_id=owner.user_id, role=owner.role)
    if not 1 <= len(task_ids) <= MAX_BULK_TASKS or len(set(task_ids)) != len(task_ids):
        raise WorkflowInputError
    tasks = tuple(
        require_task_access(
            clinic_id=clinic_id, task_id=identifier, permission="tasks.reassign"
        )
        for identifier in sorted(task_ids)
    )
    if any(task.state in {"done", "cancelled"} for task in tasks):
        raise WorkflowConflictError
    payload = {
        "clinic": str(clinic_id),
        "actor": str(actor),
        "owner_user": str(owner.user_id) if owner.user_id else None,
        "owner_role": owner.role,
        "tasks": [[str(task.pk), task.revision] for task in tasks],
    }
    return ReassignmentPreview(tasks, owner, signing.dumps(payload, salt=SALT))


@transaction.atomic
def apply_reassignment(*, clinic_id: UUID, preview_token: str) -> tuple[Task, ...]:
    """Apply only a fresh preview and current permissions, all rows or none."""
    actor = require_manager(clinic_id=clinic_id)
    if not isinstance(preview_token, str) or len(preview_token) > MAX_PREVIEW_BYTES:
        raise WorkflowInputError
    try:
        payload = signing.loads(
            preview_token, salt=SALT, max_age=PREVIEW_MAX_AGE_SECONDS
        )
    except signing.BadSignature as error:
        raise WorkflowConflictError from error
    if payload["clinic"] != str(clinic_id) or payload["actor"] != str(actor):
        raise WorkflowConflictError
    owner = TaskOwner(
        user_id=UUID(payload["owner_user"]) if payload["owner_user"] else None,
        role=payload["owner_role"],
    )
    rows = cast("list[tuple[str, int]]", payload["tasks"])
    tasks = tuple(
        require_task_access(
            clinic_id=clinic_id, task_id=UUID(identifier), permission="tasks.reassign"
        )
        for identifier, _revision in rows
    )
    if any(
        task.revision != revision
        for task, (_identifier, revision) in zip(tasks, rows, strict=True)
    ):
        raise WorkflowConflictError
    return tuple(
        assign_task(
            clinic_id=clinic_id,
            task_id=task.pk,
            owner=owner,
            expected_revision=task.revision,
        )
        for task in tasks
    )
