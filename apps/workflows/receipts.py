"""Verify real typed stored effects before committing a fenced step result."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.comms.models import IntegrationOperation
from apps.workflows.handlers import Handler, operation_digest
from apps.workflows.models import Task
from apps.workflows.validation import WorkflowInputError

if TYPE_CHECKING:
    from datetime import datetime

    from apps.workflows.handlers import HandlerResult
    from apps.workflows.models import WorkflowStep


def validate_step_receipt(
    step: WorkflowStep, result: HandlerResult, now: datetime
) -> None:
    """Read the current task, timer or transport receipt in the same transaction."""
    if result == "failed":
        if not step.error_code:
            raise WorkflowInputError
        return
    if result not in {"completed", "waiting"}:
        raise WorkflowInputError
    specification = step.run.definition_version.steps[step.position]
    kind = Handler(specification["handler"])
    completed = result == "completed"
    if not completed and step.wake_at is None:
        raise WorkflowInputError
    match kind:
        case Handler.TASK:
            task_id = step.created_task_id
            recorded = (
                task_id is not None
                and Task.objects.filter(
                    pk=task_id,
                    clinic_id=step.clinic_id,
                    idempotency_key=step.pk,
                    kind=specification["kind"],
                    subject_ref=step.run.context_refs[specification["subject"]],
                    state__in=("done",) if completed else ("assigned", "in_progress"),
                ).exists()
            )
        case Handler.TIMER:
            recorded = step.wake_at is not None and (
                step.wake_at <= now if completed else step.wake_at > now
            )
        case Handler.EXTERNAL:
            operation_id = step.operation_id
            operations = (
                IntegrationOperation.objects.filter(
                    pk=operation_id,
                    clinic_id=step.clinic_id,
                    payload_digest=operation_digest(step),
                )
                if operation_id is not None
                else IntegrationOperation.objects.none()
            )
            recorded = (
                operations.filter(status="delivered").exists()
                if completed
                else operations.exists()
            )
    if not recorded:
        raise WorkflowInputError
