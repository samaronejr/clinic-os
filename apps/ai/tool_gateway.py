"""Closed domain-command registry; there is no general-purpose execution tool."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final
from uuid import uuid5

from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.action_payload import (
    ActionPayload,
    care_window,
    closed,
    identifier,
    instant,
)
from apps.comms import agent_reminders
from apps.identity.current_context import CurrentActorError
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.services import CareActionSpec, add_care_action
from apps.workflows.services import TaskSpec, create_task
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.action_payload import JSON
    from apps.comms.agent_reminders import ReminderOutcome

REGISTERED_TOOLS: Final = MappingProxyType(
    {
        "workflows.review_task": "A1",
        "comms.synthetic_reminder": "A2",
        "care_action.create": "A1",
        "appeal.draft": "A1",
        "discrepancy.explain": "A1",
    }
)


def _care_action(*, clinic_id: UUID, data: dict[str, JSON]) -> UUID:
    """Add one administrative action through the approver's own care authority.

    The care service decides staff authority again and its database guard
    repeats it; a clinical kind is not representable here. The action key is
    derived from the reviewed proposal, so no retry can add a second action.
    """
    kind, start, end = care_window(data["parameters"])
    subject = closed(data["subject"], {"kind", "id", "expected_state", "revision"})
    sources = data["source_versions"]
    if not isinstance(sources, list):
        raise ActionRefusedError(Refusal.SOURCE)
    proposals = [
        identifier(
            closed(row, {"kind", "id", "version", "revision", "content_sha256"})["id"]
        )
        for row in sources
        if isinstance(row, dict) and row.get("kind") == "care_proposal"
    ]
    if len(proposals) != 1:
        raise ActionRefusedError(Refusal.SOURCE)
    try:
        action = add_care_action(
            clinic_id=clinic_id,
            plan_id=identifier(subject["id"]),
            spec=CareActionSpec(kind=kind, window_start=start, window_end=end),
            idempotency_key=uuid5(proposals[0], "care-action"),
        )
    except (CurrentActorError, WorkflowAccessDeniedError):
        raise ActionRefusedError(Refusal.AUTHORITY) from None
    except (WorkflowConflictError, WorkflowInputError):
        raise ActionRefusedError(Refusal.STATE) from None
    return action.pk


def execute_draft(*, clinic_id: UUID, payload: ActionPayload) -> UUID:
    """Run one A1 command through the human-authorized domain service."""
    data = payload.data()
    operation = closed(data["operation"], {"name", "version"})
    if operation["name"] in ("appeal.draft", "discrepancy.explain"):
        from apps.ai.revenue_cycle import execute_revenue_draft  # noqa: PLC0415

        return execute_revenue_draft(clinic_id=clinic_id, payload=payload)
    if operation["name"] == "care_action.create":
        return _care_action(clinic_id=clinic_id, data=data)
    if operation["name"] != "workflows.review_task":
        raise ActionRefusedError(Refusal.TOOL)
    arguments = closed(data["parameters"], {"due_at"})
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
