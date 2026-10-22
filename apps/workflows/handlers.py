"""Closed operational handlers; each outcome requires its stored effect."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from apps.comms.models import IntegrationOperation
from apps.core.integration import ActionOperationRequest, enqueue_operation
from apps.workflows.access import require_manager
from apps.workflows.task_services import assign_task, create_task
from apps.workflows.validation import TaskOwner, TaskSpec, digest

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from apps.workflows.models import WorkflowStep

type HandlerResult = Literal["completed", "waiting", "failed"]
SCAN_INTERVAL = timedelta(seconds=60)


class Handler(StrEnum):
    """Only the installed reference-only step schemas are executable."""

    TASK = "task"
    TIMER = "timer"
    EXTERNAL = "external"


def operation_digest(step: WorkflowStep) -> str:
    """Bind an action to its immutable version, position and typed references."""
    return digest(
        {
            "definition": str(step.run.definition_version_id),
            "position": step.position,
            "step": step.run.definition_version.steps[step.position],
            "refs": step.run.context_refs,
        }
    )


def _task(step: WorkflowStep, now: datetime) -> HandlerResult:
    require_manager(clinic_id=step.clinic_id)
    specification = step.run.definition_version.steps[step.position]
    task = create_task(
        clinic_id=step.clinic_id,
        spec=TaskSpec(
            kind=specification["kind"],
            subject_ref=step.run.context_refs[specification["subject"]],
            due_at=step.run.created_at
            + timedelta(seconds=specification["due_seconds"]),
        ),
        idempotency_key=step.pk,
    )
    if task.state == "open":
        task = assign_task(
            clinic_id=step.clinic_id,
            task_id=task.pk,
            owner=TaskOwner(role=specification["owner_role"]),
            expected_revision=task.revision,
        )
    step.created_task = task
    if task.state == "done":
        step.wake_at = None
        return "completed"
    if task.state == "cancelled":
        step.wake_at = None
        step.error_code = "operation_failed"
        return "failed"
    step.wake_at = now + SCAN_INTERVAL
    return "waiting"


def _timer(step: WorkflowStep, now: datetime) -> HandlerResult:
    seconds = step.run.definition_version.steps[step.position]["seconds"]
    if step.wake_at is None:
        step.wake_at = now + timedelta(seconds=seconds)
    return "completed" if step.wake_at <= now else "waiting"


def _external(step: WorkflowStep, now: datetime) -> HandlerResult:
    if step.operation_id is None:
        step.operation_id = enqueue_operation(
            ActionOperationRequest(
                provider="workflow-synthetic-v1",
                clinic_id=step.clinic_id,
                subject_type="workflows.step",
                subject_id=step.pk,
                idempotency_key=step.pk,
                payload_digest=operation_digest(step),
            )
        )
    operation = IntegrationOperation.objects.get(pk=step.operation_id)
    # Acceptance is not delivery. Only the authenticated stored callback can
    # establish this external effect; an acceptance token alone never completes.
    if operation.status == "delivered":
        return "completed"
    if operation.status in {"failed", "cancelled"}:
        step.error_code = "operation_failed"
        return "failed"
    step.error_code = "operation_unknown" if operation.status == "in_progress" else ""
    step.wake_at = now + SCAN_INTERVAL
    return "waiting"


HANDLERS: dict[str, Callable[[WorkflowStep, datetime], HandlerResult]] = {
    Handler.TASK: _task,
    Handler.TIMER: _timer,
    Handler.EXTERNAL: _external,
}
