"""Closed domain-command registry; there is no general-purpose execution tool."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.action_payload import ActionPayload, closed, instant
from apps.comms import agent_reminders
from apps.workflows.services import TaskSpec, create_task

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.agent_reminders import ReminderOutcome

REGISTERED_TOOLS: Final = MappingProxyType(
    {
        "workflows.review_task": "A1",
        "comms.synthetic_reminder": "A2",
    }
)


def execute_draft(*, clinic_id: UUID, payload: ActionPayload) -> UUID:
    """Create a review item through the human-authorized domain service."""
    data = payload.data()
    operation = closed(data["operation"], {"name", "version"})
    if operation["name"] != "workflows.review_task":
        raise ActionRefusedError(Refusal.TOOL)
    arguments = closed(data["parameters"], {"due_at"})
    from apps.ai.action_payload import identifier  # noqa: PLC0415

    task = create_task(
        clinic_id=clinic_id,
        idempotency_key=identifier(data["idempotency_key"]),
        spec=TaskSpec(
            kind="checklist",
            subject_ref={"kind": "clinic", "id": str(clinic_id)},
            due_at=instant(arguments["due_at"]),
        ),
    )
    return task.pk


def execute_external(*, provider_key: UUID, payload: ActionPayload) -> ReminderOutcome:
    """Only the reviewed offline reminder can cross this after-commit boundary."""
    data = payload.data()
    operation = closed(data["operation"], {"name", "version"})
    if operation["name"] != "comms.synthetic_reminder":
        raise ActionRefusedError(Refusal.TOOL)
    arguments = closed(data["parameters"], {"template"})
    if arguments["template"] != "logistics-v1":
        raise ActionRefusedError(Refusal.TOOL)
    return agent_reminders.send_reminder(
        provider_key=provider_key, template="logistics-v1"
    )
