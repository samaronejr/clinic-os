"""Bounded task queue and pinned overdue escalation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.workflows.access import require_clinic_access, require_task_access
from apps.workflows.models import Task
from apps.workflows.task_services import TERMINAL, _command, _save
from apps.workflows.validation import KINDS, PRIORITIES, WorkflowInputError

if TYPE_CHECKING:
    from uuid import UUID


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
    """Apply pinned escalation policy once; never substitute a new policy."""
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
