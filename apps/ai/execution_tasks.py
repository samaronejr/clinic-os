"""Dedicated reference-only dispatch; durable proposals survive broker loss."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.ai.action_contracts import ActionAccessDeniedError
from apps.ai.execution import execute_action
from apps.ai.models import ActionProposal, AgentRun
from apps.ai.tasks import execute_agent as execute
from apps.tenancy.db import service_principal_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID


def _publication(
    clinic_id: UUID, proposal_id: UUID, principal_id: UUID, queue: str
) -> Callable[[], None]:
    def publish() -> None:
        execute.apply_async(
            kwargs={
                "clinic_id": str(clinic_id),
                "proposal_id": str(proposal_id),
                "principal_id": str(principal_id),
            },
            queue=queue,
        )

    return publish


def dispatch_approved(*, clinic_id: UUID, proposal_id: UUID) -> None:
    """Apply A1 in the approving request; publish machine work after commit."""
    proposal = ActionProposal.objects.filter(
        pk=proposal_id, clinic_id=clinic_id
    ).first()
    if proposal is None:
        raise ActionAccessDeniedError
    if proposal.state != "approved":
        return
    run = AgentRun.objects.select_related("policy").get(pk=proposal.run_id)
    if run.principal_id is None:
        execute_action(clinic_id=clinic_id, proposal_id=proposal_id)
        return
    queue = "ai-batch" if run.policy.capability == "analyst" else "ai-interactive"
    transaction.on_commit(_publication(clinic_id, proposal_id, run.principal_id, queue))


def recover_approved(*, clinic_id: UUID, principal_id: UUID) -> int:
    """Republish at most 100 durable approvals for one authenticated machine."""
    with service_principal_context(principal_id=principal_id, clinic_id=clinic_id):
        rows = tuple(
            ActionProposal.objects.using("agent")
            .filter(
                clinic_id=clinic_id,
                state="approved",
            )
            .order_by("created_at")
            .values_list("pk", flat=True)[:100],
        )
    for proposal_id in rows:
        _publication(clinic_id, proposal_id, principal_id, "ai-batch")()
    return len(rows)
