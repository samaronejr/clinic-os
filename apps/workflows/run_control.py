"""Explicit failed-local-step retry with stable command and fencing history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    require_clinic_access,
    require_manager,
)
from apps.workflows.models import WorkflowRun, WorkflowStep
from apps.workflows.validation import WorkflowConflictError, digest

if TYPE_CHECKING:
    from uuid import UUID


@transaction.atomic
def retry_run(
    *, clinic_id: UUID, run_id: UUID, expected_revision: int, idempotency_key: UUID
) -> WorkflowRun:
    """Retry only failed local work, never resend or replace a provider operation.

    Scope, record, CAS revision and command identity are independent contract
    inputs; keeping them explicit matches the domain's keyword-only commands.
    """
    actor = require_manager(clinic_id=clinic_id)
    require_clinic_access(clinic_id=clinic_id, permission="tasks.assign")
    run = (
        WorkflowRun.objects.select_for_update()
        .filter(pk=run_id, clinic_id=clinic_id)
        .first()
    )
    if run is None or run.started_by_id != actor:
        raise WorkflowAccessDeniedError
    fingerprint = digest(
        {"actor": str(actor), "action": "retry", "revision": expected_revision}
    )
    if run.last_command_key == idempotency_key:
        if run.last_command_digest != fingerprint:
            raise WorkflowConflictError
        return run
    if run.revision != expected_revision or run.state != "failed":
        raise WorkflowConflictError
    steps = tuple(
        WorkflowStep.objects.select_for_update().filter(run=run).order_by("position")
    )
    failed = tuple(step for step in steps if step.state == "failed")
    if (
        not failed
        or any(step.state == "cancelled" for step in steps)
        or any(
            step.operation_id is not None or step.created_task_id is not None
            for step in failed
        )
    ):
        raise WorkflowConflictError
    run.state = "pending"
    run.revision += 1
    run.last_command_key = idempotency_key
    run.last_command_digest = fingerprint
    run.save(
        update_fields=(
            "state",
            "revision",
            "last_command_key",
            "last_command_digest",
            "updated_at",
        )
    )
    for step in failed:
        step.state = "pending"
        step.error_code = ""
        step.claim_until = step.wake_at = None
        step.save(
            update_fields=(
                "state",
                "error_code",
                "claim_until",
                "wake_at",
                "updated_at",
            )
        )
        record_phase1_event(
            "workflows.step.retried", clinic_id=clinic_id, affected_record_id=step.pk
        )
    record_phase1_event(
        "workflows.run.retried", clinic_id=clinic_id, affected_record_id=run.pk
    )
    return run
