"""A care action follows its task on every task path (todo 52, B1).

Staff work care actions where they see them: the Tasks and Inbox workspace,
bulk reassignment and the care services all drive the single ``Task``. The task
services call ``gate`` before a task moves towards completion and ``follow``
after any transition, so the action's owner and state change in the same
transaction. The database repeats both rules (``workflows_care_guard`` on
``workflows_task``), so a writer that skips these calls is refused at commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import uuid5

from apps.identity.current_context import current_actor_id
from apps.workflows.care.access import (
    keyed_digests,
    require_current_source,
    save_command,
)
from apps.workflows.care.models import CareAction
from apps.workflows.validation import WorkflowConflictError

if TYPE_CHECKING:
    from apps.workflows.models import Task

EVENTS: Final = {
    "assign": "workflows.care_action.assigned",
    "complete": "workflows.care_action.completed",
    "cancel": "workflows.care_action.cancelled",
}


def gate(task: Task) -> None:
    """Refuse to assign, start or complete a care task awaiting its clinician.

    A flagged action waits for the clinician's review; an unflagged action whose
    source left 'finalized' is refused until confirmed, before any scan runs.
    """
    action = CareAction.objects.filter(task_id=task.pk).first()
    if action is None:
        return
    if action.review_state == "required":
        raise WorkflowConflictError
    require_current_source(action)


def follow(task: Task, verb: str) -> None:
    """Mirror the task's owner and state onto its care action, if it has one.

    The command key derives from the action's revision exactly as the care
    services bind it, so a care command and the same command issued from the
    task workspace replay as one.
    """
    action = CareAction.objects.select_for_update().filter(task_id=task.pk).first()
    if action is None:
        return
    state = action.state
    if task.state in {"done", "cancelled"}:
        state = task.state
    elif task.state in {"assigned", "in_progress"} and action.state == "open":
        state = "assigned"
    if (action.owner_user_id, action.owner_role, action.state) == (
        task.owner_user_id,
        task.owner_role,
        state,
    ):
        return
    terms: dict[str, object] = {}
    if verb == "assign":
        terms = {
            "user": str(task.owner_user_id) if task.owner_user_id else None,
            "role": task.owner_role,
        }
    elif verb == "complete":
        terms = {"outcome": task.completion_evidence["outcome"]}
    action.last_command_key = uuid5(action.pk, f"{verb}:{action.revision}")
    action.last_command_digest = keyed_digests(
        {"actor": str(current_actor_id()), "terms": terms}
    )[-1]
    action.owner_user_id = task.owner_user_id
    action.owner_role = task.owner_role
    action.state = state
    save_command(action, ("owner_user_id", "owner_role", "state"), EVENTS[verb])
