"""Query ambiguous outcomes and run only the compensation bound into approval."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid5

from apps.ai.action_contracts import ActionRefusedError, ExecutionResult, Refusal
from apps.ai.action_payload import closed
from apps.ai.action_runtime import action_scope, send_custody
from apps.ai.action_store import load_action, record_receipt
from apps.ai.action_validation import validate_current
from apps.ai.approvals import verify_approvals
from apps.ai.execution import set_execution_state
from apps.ai.models import ActionExecution, ActionReceipt, Compensation
from apps.ai.proposals import transition
from apps.audit.services import record_phase1_event
from apps.comms import agent_reminders

if TYPE_CHECKING:
    from uuid import UUID


def reconcile_action(
    *, clinic_id: UUID, proposal_id: UUID, principal_id: UUID
) -> ExecutionResult:
    """Reconcile with the original provider key; this path cannot call send."""
    with send_custody(clinic_id=clinic_id, principal_id=principal_id) as using:
        with action_scope(clinic_id=clinic_id, principal_id=principal_id):
            proposal, _payload, _policy = load_action(
                clinic_id=clinic_id,
                proposal_id=proposal_id,
                using=using,
            )
            execution = (
                ActionExecution.objects.using(using)
                .select_for_update()
                .filter(proposal=proposal)
                .first()
            )
            if execution is None:
                raise ActionRefusedError(Refusal.STATE)
            if execution.state != "unknown":
                receipt = (
                    ActionReceipt.objects.using(using)
                    .filter(execution=execution, kind="reconciliation")
                    .first()
                )
                if receipt is None:
                    raise ActionRefusedError(Refusal.STATE)
                return ExecutionResult(
                    proposal.pk, execution.state, execution.pk, receipt.pk
                )
            execution.fencing_token += 1
            execution.save(using=using, update_fields=("fencing_token",))
        result = agent_reminders.reconcile_reminder(provider_key=execution.provider_key)
        agent_reminders.authenticate_outcome(
            result=result, provider_key=execution.provider_key
        )
        with action_scope(clinic_id=clinic_id, principal_id=principal_id):
            current = (
                ActionExecution.objects.using(using)
                .select_for_update()
                .get(pk=execution.pk)
            )
            if (
                current.fencing_token != execution.fencing_token
                or current.state != "unknown"
            ):
                raise ActionRefusedError(Refusal.FENCE)
            if result.outcome == "unknown":
                return ExecutionResult(
                    proposal.pk, "unknown", current.pk, reason=Refusal.OUTCOME
                )
            receipt = record_receipt(
                execution=current,
                kind="reconciliation",
                outcome=result.outcome,
                reference=result.reference,
                using=using,
            )
            state = "reconciled" if result.outcome == "succeeded" else "exception"
            set_execution_state(current, state, using=using)
            transition(proposal, state, using=using)
            return ExecutionResult(proposal.pk, state, current.pk, receipt.pk)


def compensate_action(
    *, clinic_id: UUID, proposal_id: UUID, principal_id: UUID
) -> ExecutionResult:
    """Run the bound strategy once; leave uncertain compensation for a human."""
    with send_custody(clinic_id=clinic_id, principal_id=principal_id) as using:
        with action_scope(clinic_id=clinic_id, principal_id=principal_id):
            proposal, payload, policy = load_action(
                clinic_id=clinic_id,
                proposal_id=proposal_id,
                using=using,
            )
            execution = (
                ActionExecution.objects.using(using)
                .select_for_update()
                .filter(proposal=proposal)
                .first()
            )
            if execution is None:
                raise ActionRefusedError(Refusal.STATE)
            prior = (
                Compensation.objects.using(using).filter(execution=execution).first()
            )
            if prior is not None:
                return ExecutionResult(proposal.pk, prior.state, execution.pk)
            if execution.state != "failure":
                raise ActionRefusedError(Refusal.STATE)
            terms = closed(payload.data()["compensation_policy"], {"mode", "version"})
            compensation = Compensation.objects.using(using).create(
                organization_id=execution.organization_id,
                clinic_id=clinic_id,
                execution=execution,
                provider_key=uuid5(execution.provider_key, "compensation-v1"),
                policy_version=1,
            )
            record_phase1_event(
                "ai.compensation.compensating",
                clinic_id=clinic_id,
                affected_record_id=compensation.pk,
            )
            set_execution_state(execution, "compensating", using=using)
            transition(proposal, "compensating", using=using)
            permitted = terms["mode"] == "synthetic_cancel"
            try:
                verify_approvals(proposal=proposal, payload=payload, using=using)
                validate_current(
                    payload=payload, policy=policy, proposal_id=proposal_id, using=using
                )
            except ActionRefusedError:
                permitted = False
        result = None
        if permitted:
            try:
                result = agent_reminders.cancel_reminder(
                    provider_key=compensation.provider_key,
                    original_key=execution.provider_key,
                )
                agent_reminders.authenticate_outcome(
                    result=result, provider_key=compensation.provider_key
                )
            except (TimeoutError, ConnectionError, ActionRefusedError):
                result = None
        with action_scope(clinic_id=clinic_id, principal_id=principal_id):
            current = (
                ActionExecution.objects.using(using)
                .select_for_update()
                .get(pk=execution.pk)
            )
            if (
                current.fencing_token != execution.fencing_token
                or current.state != "compensating"
            ):
                raise ActionRefusedError(Refusal.FENCE)
            state = (
                "compensated"
                if result is not None and result.outcome == "cancelled"
                else "manual_required"
            )
            receipt = None
            if result is not None:
                receipt = record_receipt(
                    execution=current,
                    kind="compensation",
                    outcome=result.outcome,
                    reference=result.reference,
                    using=using,
                )
            compensation.state = state
            compensation.save(using=using, update_fields=("state",))
            record_phase1_event(
                f"ai.compensation.{state}",
                clinic_id=clinic_id,
                affected_record_id=compensation.pk,
            )
            set_execution_state(current, state, using=using)
            transition(proposal, state, using=using)
            return ExecutionResult(
                proposal.pk, state, current.pk, receipt.pk if receipt else None
            )
