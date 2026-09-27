"""Explicit synthetic action transport; never a generic HTTP or clinical tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import connection

from apps.comms.adapters import PermanentSendError, SendResult
from apps.comms.models import IntegrationOperation
from apps.core.integration import register_action_adapter, register_subject_recheck
from apps.identity.current_context import CurrentActorError, require_permission
from apps.workflows.engine import operation_digest
from apps.workflows.models import WorkflowStep

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.adapters import OperationScope


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """Only the approved input digest crosses the transaction boundary."""

    digest: str


def step_send_eligible(scope: OperationScope) -> bool:
    """Revalidate the current actor, pinned payload and still-live run before send."""
    try:
        actor = require_permission("tasks.reassign", clinic_id=scope.clinic_id)
        require_permission("tasks.assign", clinic_id=scope.clinic_id)
    except CurrentActorError:
        return False
    operation = IntegrationOperation.objects.filter(
        pk=scope.operation_id, clinic_id=scope.clinic_id
    ).first()
    if operation is None:
        return False
    step = (
        WorkflowStep.objects.select_related("run__definition_version")
        .filter(
            pk=operation.subject_id,
            clinic_id=scope.clinic_id,
            operation_id=operation.pk,
        )
        .first()
    )
    return (
        step is not None
        and actor == scope.actor_id == step.run.started_by_id
        and step.state in {"running", "waiting"}
        and step.run.state in {"running", "waiting"}
        and operation.payload_digest == operation_digest(step)
        and step.run.definition_version.steps[step.position]
        == {"handler": "external", "provider": "workflow-synthetic-v1"}
        and settings.CLINIC_DATA_MODE == "synthetic"
    )


class SyntheticWorkflowAdapter:
    """A deterministic rehearsal receipt, not a claim of real provider execution."""

    provider = "workflow-synthetic-v1"

    def prepare(self, operation: IntegrationOperation) -> PreparedAction:
        """Require the outbox subject recheck and exact canonical digest."""
        from apps.comms.adapters import OperationScope  # noqa: PLC0415

        scope = OperationScope(
            operation_id=operation.pk,
            organization_id=operation.organization_id,
            clinic_id=operation.clinic_id,
            actor_id=operation.actor_id,
        )
        if not step_send_eligible(scope):
            raise PermanentSendError
        return PreparedAction(operation.payload_digest)

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return the same synthetic acceptance identity for duplicate deliveries."""
        if (
            not isinstance(prepared, PreparedAction)
            or connection.in_atomic_block
            or settings.CLINIC_DATA_MODE != "synthetic"
        ):
            raise PermanentSendError
        return SendResult(provider_reference=f"synthetic:workflow:{operation_id}")


def register_adapters() -> None:
    """Register only the reviewed synthetic transport and its mandatory recheck."""
    register_action_adapter(SyntheticWorkflowAdapter())
    register_subject_recheck("workflows.step", step_send_eligible)
