"""Reference-only media work on the governed AI-interactive queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from celery import shared_task

from apps.core.fairness import DEFER_COUNTDOWN_SECONDS, fair_acquire
from apps.core.integration import ExecutionResult, _resolve_scope, execute_operation

if TYPE_CHECKING:
    from apps.comms.adapters import OperationScope


class TaskSender(Protocol):
    """Only the stored outbox identifier enters the broker."""

    def apply_async(
        self, *, kwargs: dict[str, str], queue: str, countdown: int = 0
    ) -> None:
        """Publish one reference-only broker message."""


def dispatch(scope: OperationScope) -> None:
    """Publish the stored outbox reference after the outer transaction commits."""
    execute.apply_async(
        kwargs={"operation_id": str(scope.operation_id)}, queue="ai-interactive"
    )


def run_operation(*, operation_id: str) -> ExecutionResult:
    """Evaluate fairness from stored tenant provenance, never broker claims."""
    scope = _resolve_scope(UUID(operation_id))
    if scope is None:
        return "missing"
    if not fair_acquire(organization_id=scope.organization_id, queue="ai-interactive"):
        execute.apply_async(
            kwargs={"operation_id": operation_id},
            queue="ai-interactive",
            countdown=DEFER_COUNTDOWN_SECONDS,
        )
        return "retry"
    return execute_operation(scope.operation_id)


execute: TaskSender = shared_task(name="apps.scribe.tasks.execute")(run_operation)
