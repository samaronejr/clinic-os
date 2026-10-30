"""Reference-only AI queue admission, including recovered outbox operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from celery import shared_task

from apps.ai.action_contracts import ActionAccessDeniedError
from apps.ai.completion import finish_failure
from apps.ai.contracts import Purpose
from apps.ai.execution import execute_action
from apps.ai.models import ActionProposal, AgentRun, AIInvocation
from apps.comms.models import IntegrationOperation
from apps.core.fairness import DEFER_COUNTDOWN_SECONDS, fair_acquire
from apps.core.integration import (
    ExecutionResult,
    _resolve_scope,
    _stored_scope_context,
    execute_operation,
)
from apps.tenancy.db import service_principal_context

if TYPE_CHECKING:
    from apps.comms.adapters import OperationScope


class _TaskSender(Protocol):
    """The typed Celery publication surface; only opaque ids enter the broker."""

    def apply_async(
        self, *, kwargs: dict[str, str], queue: str, countdown: int = 0
    ) -> None: ...


def _queue(scope: OperationScope) -> str:
    """Derive routing from committed provenance, never from a broker claim."""
    with _stored_scope_context(scope):
        purpose = (
            AIInvocation.objects.filter(
                operation_id=scope.operation_id, clinic_id=scope.clinic_id
            )
            .values_list("purpose", flat=True)
            .first()
        )
    return (
        "ai-batch"
        if purpose in {Purpose.ANALYST.value, Purpose.SCRIBE_DRAFT.value}
        else "ai-interactive"
    )


def dispatch(scope: OperationScope) -> None:
    """Publish one reference after commit, with the correct dedicated queue."""
    execute.apply_async(
        kwargs={"operation_id": str(scope.operation_id)}, queue=_queue(scope)
    )


def run_operation(*, operation_id: str) -> ExecutionResult:
    """Apply organization fairness before claiming or decrypting any input."""
    scope = _resolve_scope(UUID(operation_id))
    if scope is None:
        return "missing"
    queue = _queue(scope)
    if not fair_acquire(organization_id=scope.organization_id, queue=queue):
        execute.apply_async(
            kwargs={"operation_id": operation_id},
            queue=queue,
            countdown=DEFER_COUNTDOWN_SECONDS,
        )
        return "retry"
    result = execute_operation(scope.operation_id)
    # Core may cancel before a domain recheck (for example, revoked staff).
    # Finish only content-free bookkeeping under the stored operation scope.
    with _stored_scope_context(scope):
        operation = IntegrationOperation.objects.get(pk=scope.operation_id)
        if operation.status in {"cancelled", "failed"}:
            invocation = (
                AIInvocation.objects.select_for_update()
                .filter(operation_id=scope.operation_id, status="reserved")
                .first()
            )
            if invocation is not None:
                finish_failure(
                    invocation,
                    reason=(
                        "authority_revoked"
                        if operation.status == "cancelled"
                        else "configuration_unavailable"
                        if operation.last_error
                        in {"adapter_unavailable", "prepare_failed"}
                        else "provider_unavailable"
                    ),
                )
    return result


execute: _TaskSender = shared_task(name="apps.ai.tasks.execute")(run_operation)


def run_agent(*, clinic_id: str, proposal_id: str, principal_id: str) -> str:
    """Reauthenticate the exact database principal before fairness or execution."""
    clinic, proposal, principal = UUID(clinic_id), UUID(proposal_id), UUID(principal_id)
    with service_principal_context(principal_id=principal, clinic_id=clinic):
        action = (
            ActionProposal.objects.using("agent")
            .defer("payload")
            .filter(pk=proposal, clinic_id=clinic)
            .first()
        )
        if action is None:
            raise ActionAccessDeniedError
        run = (
            AgentRun.objects.using("agent")
            .defer("contract", "reviewer_edits")
            .select_related("policy")
            .get(pk=action.run_id)
        )
        organization = action.organization_id
        queue = "ai-batch" if run.policy.capability == "analyst" else "ai-interactive"
    if not fair_acquire(organization_id=organization, queue=queue):
        execute_agent.apply_async(
            kwargs={
                "clinic_id": clinic_id,
                "proposal_id": proposal_id,
                "principal_id": principal_id,
            },
            queue=queue,
            countdown=DEFER_COUNTDOWN_SECONDS,
        )
        return "deferred"
    return execute_action(
        clinic_id=clinic, proposal_id=proposal, principal_id=principal
    ).state


execute_agent: _TaskSender = shared_task(name="apps.ai.tasks.execute_agent")(run_agent)
