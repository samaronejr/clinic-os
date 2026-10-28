"""The single AP eight-step execute-time algorithm for registered agent commands."""

from __future__ import annotations

from datetime import timedelta
from time import monotonic_ns
from typing import TYPE_CHECKING

from django.db.models import Sum

from apps.ai import tool_gateway
from apps.ai.action_contracts import ActionRefusedError, ExecutionResult, Refusal
from apps.ai.action_payload import closed, identifier
from apps.ai.action_runtime import action_scope, send_custody
from apps.ai.action_store import load_action, record_receipt
from apps.ai.action_validation import database_time, validate_current
from apps.ai.approvals import verify_approvals
from apps.ai.models import ActionExecution, ActionProposal, ActionReceipt, AgentRun
from apps.ai.proposals import transition
from apps.audit.services import record_phase1_event
from apps.comms.agent_reminders import authenticate_outcome

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.action_payload import ActionPayload
    from apps.ai.models import AgentPolicyVersion


def set_execution_state(
    execution: ActionExecution, state: str, *, reason: str = "", using: str
) -> None:
    """Persist one legal execution edge, retaining its immutable send identity."""
    execution.state, execution.reason_code = state, reason
    execution.save(using=using, update_fields=("state", "reason_code"))
    record_phase1_event(
        f"ai.execution.{state}",
        clinic_id=execution.clinic_id,
        affected_record_id=execution.pk,
    )


def _existing(
    execution: ActionExecution, proposal: ActionProposal, using: str
) -> ExecutionResult:
    """Treat an abandoned send as ambiguous without dispatching it again."""
    if execution.state == "executing":
        set_execution_state(
            execution, "unknown", reason=Refusal.OUTCOME.value, using=using
        )
        transition(proposal, "unknown", reason=Refusal.OUTCOME.value, using=using)
    receipt = (
        ActionReceipt.objects.using(using)
        .filter(execution=execution)
        .order_by("-created_at")
        .first()
    )
    return ExecutionResult(
        proposal.pk,
        execution.state,
        execution.pk,
        receipt.pk if receipt else None,
        Refusal.OUTCOME if execution.state == "unknown" else None,
    )


def _reserve(
    *,
    proposal: ActionProposal,
    payload: ActionPayload,
    policy: AgentPolicyVersion,
    using: str,
) -> ActionExecution:
    """Verify approval and current controls, then atomically reserve the intent."""
    if proposal.state != "approved":
        raise ActionRefusedError(Refusal.STATE)
    data = payload.data()
    approval_policy = closed(
        data["approval_policy"],
        {"policy_id", "version", "quorum", "autonomy"},
    )
    if (using == "default") != (approval_policy["autonomy"] == "A1"):
        raise ActionRefusedError(Refusal.AUTHORITY)
    # 4. Recompute canonical digest, verify all attestations and distinct quorum.
    verify_approvals(proposal=proposal, payload=payload, using=using)
    # 5. Current authority, versions, budgets and DB-time invariants.
    validate_current(
        payload=payload,
        policy=policy,
        proposal_id=proposal.pk,
        using=using,
    )
    now = database_time(using=using)
    budget_rows = ActionExecution.objects.using(using).filter(
        clinic_id=proposal.clinic_id,
        proposal__run__policy__capability=policy.capability,
        intent_at__gte=now.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    reserved = budget_rows.aggregate(total=Sum("reserved_micros"))["total"] or 0
    if (
        budget_rows.count() >= policy.budget_actions
        or reserved + 1 > policy.budget_micros
    ):
        raise ActionRefusedError(Refusal.BUDGET)
    # 6. Reserve and persist intent atomically, before external work.
    execution = ActionExecution.objects.using(using).create(
        organization_id=proposal.organization_id,
        clinic_id=proposal.clinic_id,
        proposal=proposal,
        digest=payload.digest,
        provider_key=identifier(data["idempotency_key"]),
        fencing_token=1,
        intent_at=now,
        deadline=now + timedelta(seconds=policy.timeout_seconds),
        reserved_micros=1,
    )
    transition(proposal, "executing", using=using)
    record_phase1_event(
        "ai.execution.executing",
        clinic_id=proposal.clinic_id,
        affected_record_id=execution.pk,
    )
    return execution


def _mark_send(
    *,
    proposal: ActionProposal,
    payload: ActionPayload,
    policy: AgentPolicyVersion,
    execution: ActionExecution,
    using: str,
) -> None:
    """Recheck at the actual send boundary under the retained mutation lock."""
    verify_approvals(proposal=proposal, payload=payload, using=using)
    validate_current(
        payload=payload,
        policy=policy,
        proposal_id=proposal.pk,
        using=using,
    )
    if database_time(using=using) >= execution.deadline:
        raise ActionRefusedError(Refusal.EXPIRED)
    execution.send_started_at = database_time(using=using)
    execution.save(using=using, update_fields=("send_started_at",))
    record_phase1_event(
        "ai.execution.sending",
        clinic_id=proposal.clinic_id,
        affected_record_id=execution.pk,
    )


def execute_action(
    *, clinic_id: UUID, proposal_id: UUID, principal_id: UUID | None = None
) -> ExecutionResult:
    """Execute the AP algorithm once.

    Authenticate -> claim -> lock/reload -> verify -> revalidate -> reserve
    -> send-boundary recheck -> authenticated receipt.

    No retry path can reach the tool after a committed execution intent.
    A1 has an existing human transaction and only a private review command.
    A2/A3 use service_principal_context, and external simulation is after commit.
    """
    # 1. Authenticate the executor before any selector lookup or session lock.
    with send_custody(clinic_id=clinic_id, principal_id=principal_id) as using:
        try:
            with action_scope(clinic_id=clinic_id, principal_id=principal_id):
                # 2. Claim the unique digest's execution identity under custody.
                # 3. Ordered mutation lock precedes proposal/source reload.
                proposal, payload, policy = load_action(
                    clinic_id=clinic_id,
                    proposal_id=proposal_id,
                    using=using,
                )
                existing = (
                    ActionExecution.objects.using(using)
                    .filter(proposal=proposal)
                    .first()
                )
                if existing is not None:
                    return _existing(existing, proposal, using)
                execution = _reserve(
                    proposal=proposal,
                    payload=payload,
                    policy=policy,
                    using=using,
                )
            with action_scope(clinic_id=clinic_id, principal_id=principal_id):
                # 7. Recheck at the actual send boundary while mutation custody
                # is retained; no human identity is installed on the agent alias.
                proposal, payload, policy = load_action(
                    clinic_id=clinic_id,
                    proposal_id=proposal_id,
                    using=using,
                )
                _mark_send(
                    proposal=proposal,
                    payload=payload,
                    policy=policy,
                    execution=execution,
                    using=using,
                )
                if using == "default":
                    reference = tool_gateway.execute_draft(
                        clinic_id=clinic_id, payload=payload
                    )
                    draft_receipt = record_receipt(
                        execution=execution,
                        kind="execution",
                        outcome="succeeded",
                        reference=reference,
                        using=using,
                    )
                    set_execution_state(execution, "receipt", using=using)
                    transition(proposal, "receipt", using=using)
                    return ExecutionResult(
                        proposal.pk, "receipt", execution.pk, draft_receipt.pk
                    )
        except ActionRefusedError as error:
            with action_scope(clinic_id=clinic_id, principal_id=principal_id):
                rejected = (
                    ActionProposal.objects.using(using)
                    .defer("payload")
                    .filter(
                        pk=proposal_id,
                        clinic_id=clinic_id,
                    )
                    .first()
                )
                if rejected is not None and rejected.state in ("approved", "executing"):
                    pending = (
                        ActionExecution.objects.using(using)
                        .filter(proposal=rejected)
                        .first()
                    )
                    if pending is not None:
                        set_execution_state(
                            pending, "rejected", reason=error.reason.value, using=using
                        )
                    transition(
                        rejected, "rejected", reason=error.reason.value, using=using
                    )
            return ExecutionResult(proposal_id, "rejected", reason=error.reason)

        started = monotonic_ns()
        outcome = None
        try:
            outcome = tool_gateway.execute_external(
                provider_key=execution.provider_key, payload=payload
            )
            authenticate_outcome(result=outcome, provider_key=execution.provider_key)
        except (TimeoutError, ConnectionError, ActionRefusedError):
            outcome = None
        latency_ms = min((monotonic_ns() - started) // 1_000_000, 2**31 - 1)
        if latency_ms > policy.timeout_seconds * 1000:
            outcome = None
        with action_scope(clinic_id=clinic_id, principal_id=principal_id):
            # 8. Persist only authenticated outcomes against the exact fence.
            current = (
                ActionExecution.objects.using(using)
                .select_for_update()
                .get(pk=execution.pk)
            )
            if (
                current.fencing_token != execution.fencing_token
                or current.state != "executing"
            ):
                raise ActionRefusedError(Refusal.FENCE)
            receipt = None
            state = "unknown"
            if outcome is not None:
                receipt = record_receipt(
                    execution=current,
                    kind="execution",
                    outcome=outcome.outcome,
                    reference=outcome.reference,
                    using=using,
                )
                state = {
                    "succeeded": "receipt",
                    "failed": "failure",
                    "unknown": "unknown",
                    "cancelled": "failure",
                }[outcome.outcome]
            reason = Refusal.OUTCOME.value if state == "unknown" else ""
            set_execution_state(current, state, reason=reason, using=using)
            transition(proposal, state, reason=reason, using=using)
            AgentRun.objects.using(using).filter(pk=proposal.run_id).update(
                cost_micros=1, latency_ms=latency_ms
            )
            return ExecutionResult(
                proposal.pk, state, current.pk, receipt.pk if receipt else None
            )
