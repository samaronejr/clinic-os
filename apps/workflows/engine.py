"""Session-locked, fenced workflow execution with transactional local effects."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, assert_never, cast

from django.db import connection, connections, transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.workflows import handlers
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.handlers import Handler
from apps.workflows.models import WorkflowRun, WorkflowStep
from apps.workflows.receipts import validate_step_receipt
from apps.workflows.validation import (
    WorkflowConflictError,
    WorkflowInputError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

Result = Literal[
    "completed", "waiting", "failed", "cancelled", "stale", "busy", "denied", "missing"
]
CLAIM_LIFETIME = timedelta(minutes=2)
HANDLERS = handlers.HANDLERS


def utc_now() -> datetime:
    """Use database time in production; tests inject this exact clock seam."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.workflows_now()")
        row = cursor.fetchone()
    return cast("datetime", row[0])


def _step_scope(step_id: UUID) -> tuple[UUID, UUID, UUID] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT organization_id,clinic_id,actor_id "
            "FROM clinic_app.workflows_step_scope(%s)",
            [step_id],
        )
        row = cursor.fetchone()
    return None if row is None else (row[0], row[1], row[2])


@contextmanager
def _step_lock(step_id: UUID) -> Iterator[bool]:
    """Hold a dedicated connection's lock across claim/effect commits."""
    lock_connection = connections.create_connection("default")
    try:
        with lock_connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(pg_catalog.hashtextextended(%s,0))",
                [f"clinic-workflow-step-v1:{step_id}"],
            )
            row = cursor.fetchone()
        yield row == (True,)
    finally:
        lock_connection.close()


def _locked_step(step_id: UUID) -> WorkflowStep:
    step = WorkflowStep.objects.filter(pk=step_id).first()
    if step is None:
        raise WorkflowAccessDeniedError
    actor = require_permission("tasks.reassign", clinic_id=step.clinic_id)
    require_permission("tasks.assign", clinic_id=step.clinic_id)
    # All commands lock run before step, including cancellation: no lock inversion.
    run = WorkflowRun.objects.select_for_update().get(pk=step.run_id)
    if run.started_by_id != actor:
        raise WorkflowAccessDeniedError
    return (
        WorkflowStep.objects.select_for_update(of=("self",))
        .select_related("run__definition_version")
        .get(pk=step_id)
    )


def _step_event(step: WorkflowStep, verb: str) -> None:
    record_phase1_event(
        f"workflows.step.{verb}", clinic_id=step.clinic_id, affected_record_id=step.pk
    )


def _run_state(run: WorkflowRun, state: str) -> None:
    if run.state != state:
        run.state = state
        run.revision += 1
        run.save(update_fields=("state", "revision", "updated_at"))
        record_phase1_event(
            f"workflows.run.{state}", clinic_id=run.clinic_id, affected_record_id=run.pk
        )


@transaction.atomic
def claim_step(*, step_id: UUID) -> int | Result:
    """Increment the fencing generation only after rechecking stored authority."""
    step = _locked_step(step_id)
    if step.state in {"completed", "failed", "cancelled"}:
        return cast("Result", step.state)
    if step.run.state in {"cancelled", "failed", "completed"}:
        return "cancelled"
    now = utc_now()
    if (
        step.state == "running"
        and step.claim_until is not None
        and step.claim_until > now
    ):
        return "busy"
    if step.wake_at is not None and step.wake_at > now:
        return "waiting"
    if (
        WorkflowStep.objects.filter(run=step.run, position__lt=step.position)
        .exclude(state="completed")
        .exists()
    ):
        return "waiting"
    step.fencing_token += 1
    step.state = "running"
    step.claim_until = now + CLAIM_LIFETIME
    step.save(update_fields=("state", "fencing_token", "claim_until", "updated_at"))
    _run_state(step.run, "running")
    _step_event(step, "claimed")
    return step.fencing_token


@transaction.atomic
def apply_claim(*, step_id: UUID, fencing_token: int) -> Result:
    """Fence before the handler; local effects and completion commit together."""
    step = _locked_step(step_id)
    if step.fencing_token != fencing_token or step.state != "running":
        return "stale"
    if step.run.state != "running":
        return "cancelled"
    specification = step.run.definition_version.steps[step.position]
    kind = Handler(specification["handler"])
    result = HANDLERS[kind](step, utc_now())
    validate_step_receipt(step, result, utc_now())
    step.state = result
    step.claim_until = None
    step.save(
        update_fields=(
            "state",
            "claim_until",
            "wake_at",
            "operation_id",
            "created_task_id",
            "error_code",
            "updated_at",
        )
    )
    _step_event(step, result)
    match result:
        case "failed" | "waiting":
            _run_state(step.run, result)
        case "completed":
            if (
                not WorkflowStep.objects.filter(run=step.run)
                .exclude(state="completed")
                .exists()
            ):
                _run_state(step.run, "completed")
        case unreachable_result:
            assert_never(unreachable_result)
    return result


def _apply_or_fail(step_id: UUID, claim: int) -> Result:
    try:
        return apply_claim(step_id=step_id, fencing_token=claim)
    except (WorkflowInputError, WorkflowConflictError):
        # The handler's savepoint rolls back all effects before this receipt.
        step = _locked_step(step_id)
        if step.fencing_token != claim:
            return "stale"
        step.state = "failed"
        step.error_code = "handler_rejected"
        step.claim_until = None
        step.save(update_fields=("state", "error_code", "claim_until", "updated_at"))
        _step_event(step, "failed")
        _run_state(step.run, "failed")
        return "failed"


def execute_step(*, step_id: UUID) -> Result:
    """Resolve stored scope, own the claim lock, and re-enter tenant transactions."""
    from ops.release.activation import require_live_runtime  # noqa: PLC0415

    require_live_runtime(os.environ)
    scope = _step_scope(step_id)
    if scope is None:
        return "missing"
    organization, _clinic, actor = scope
    with _step_lock(step_id) as acquired:
        if not acquired:
            return "busy"
        try:
            with tenant_context(actor, organization):
                claim = claim_step(step_id=step_id)
            if not isinstance(claim, int):
                return claim
            with tenant_context(actor, organization):
                return _apply_or_fail(step_id, claim)
        except (CurrentActorError, TenantAccessDeniedError):
            return "denied"


def due_steps() -> tuple[UUID, ...]:
    """Beat enumerates only opaque due IDs; execution rechecks all authority."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.workflows_due_steps(%s)", [utc_now()])
        return tuple(row[0] for row in cursor.fetchall())
