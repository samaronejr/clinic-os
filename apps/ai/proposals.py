"""Typed proposals retain provenance; untrusted content never becomes a command."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import JSON, ActionPayload, closed, identifier
from apps.ai.action_validation import validate_current
from apps.ai.contracts import Purpose
from apps.ai.models import ActionProposal, AgentPolicyVersion, AgentRun
from apps.ai.policy import check_configuration, select_configuration
from apps.ai.prompt_registry import load_prompt
from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.untrusted import UntrustedContent


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Inspectable AI-10 facts; model reasoning has no field in this contract."""

    sources: tuple[UntrustedContent, ...] = ()
    trigger: str = "manual"
    evaluation_set: str = "synthetic-agent-v1"


DEFAULT_CONTEXT = AgentContext()


def transition(
    proposal: ActionProposal, state: str, *, reason: str = "", using: str = "default"
) -> None:
    """Advance one DB-enforced edge and append its fixed audit events."""
    proposal.state, proposal.reason_code = state, reason
    proposal.save(using=using, update_fields=("state", "reason_code"))
    AgentRun.objects.using(using).filter(pk=proposal.run_id).update(
        outcome=state,
        reason_code=reason,
    )
    record_phase1_event(
        f"ai.agent_run.{state}",
        clinic_id=proposal.clinic_id,
        affected_record_id=proposal.run_id,
    )
    record_phase1_event(
        f"ai.action.{state}",
        clinic_id=proposal.clinic_id,
        affected_record_id=proposal.pk,
    )


@transaction.atomic
def propose_action(
    *, clinic_id: UUID, payload: ActionPayload, context: AgentContext = DEFAULT_CONTEXT
) -> ActionProposal:
    """Persist a canonical model output separately from its untrusted input data."""
    actor = current_actor_id()
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.agent_staff(%s)", [clinic_id])
        if cursor.fetchone() != (True,):
            raise ActionAccessDeniedError
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-send-v1',0))"
        )
    data = payload.data()
    terms = closed(
        data["approval_policy"], {"policy_id", "version", "quorum", "autonomy"}
    )
    policy = AgentPolicyVersion.objects.filter(
        pk=identifier(terms["policy_id"]),
        clinic_id=clinic_id,
    ).first()
    if policy is None:
        raise ActionAccessDeniedError
    previous = ActionProposal.objects.filter(
        clinic_id=clinic_id,
        idempotency_key=identifier(data["idempotency_key"]),
    ).first()
    if previous is not None:
        if previous.digest != payload.digest:
            raise ActionRefusedError(Refusal.DIGEST)
        return previous
    if (
        context.trigger not in ("manual", "workflow")
        or context.evaluation_set != "synthetic-agent-v1"
    ):
        raise ActionRefusedError(Refusal.POLICY)
    configuration = select_configuration(
        clinic_id=clinic_id, purpose=Purpose(policy.capability)
    )
    check_configuration(configuration, clinic_id)
    _prompt, prompt_hash = load_prompt(Purpose(policy.capability))
    contract: dict[str, JSON] = {
        "trigger": context.trigger,
        "principal": str(policy.principal_id) if policy.principal_id else str(actor),
        "context_sources": data["source_versions"],
        "intended_output": data["operation"],
        "allowed_tools": policy.tool_allowlist,
        "data_scope": {"clinic_id": str(clinic_id), "patient_id": data["patient_id"]},
        "autonomy_ceiling": policy.autonomy_ceiling,
        "authorization": data["approval_policy"],
        "validation": "action-payload-v1",
        "timeout_seconds": policy.timeout_seconds,
        "budget": {"actions": policy.budget_actions, "micros": policy.budget_micros},
        "audit": "agent-action-v1",
        "compensation": policy.compensation_policy,
        "escalation": policy.escalation_queue,
        "evaluation_set": context.evaluation_set,
        "model": {
            "provider": configuration.provider,
            "id": configuration.model_id,
            "version": configuration.version,
        },
        "prompt_sha256": prompt_hash,
        "source_consent_basis": "synthetic-only",
        "untrusted_sources": [
            {
                "origin": source.origin,
                "content_sha256": hashlib.sha256(source.text.encode()).hexdigest(),
                "flagged": source.flagged,
            }
            for source in context.sources
        ],
    }
    run = AgentRun.objects.create(
        organization_id=policy.organization_id,
        clinic_id=clinic_id,
        policy=policy,
        user_id=actor,
        principal_id=policy.principal_id,
        configuration=configuration,
        prompt_sha256=prompt_hash,
        trigger=context.trigger,
        contract=contract,
        evaluation_set=context.evaluation_set,
    )
    proposal = ActionProposal.objects.create(
        id=identifier(data["action_id"]),
        organization_id=policy.organization_id,
        clinic_id=clinic_id,
        run=run,
        payload=payload.canonical.decode(),
        digest=payload.digest,
        idempotency_key=identifier(data["idempotency_key"]),
    )
    record_phase1_event(
        "ai.agent_run.created", clinic_id=clinic_id, affected_record_id=run.pk
    )
    record_phase1_event(
        "ai.action.proposal", clinic_id=clinic_id, affected_record_id=proposal.pk
    )
    if any(source.flagged for source in context.sources):
        transition(proposal, "rejected", reason=Refusal.INJECTION.value)
        return proposal
    try:
        validate_current(payload=payload, policy=policy, proposal_id=proposal.pk)
    except ActionRefusedError as error:
        transition(proposal, "rejected", reason=error.reason.value)
        return proposal
    transition(proposal, "validated")
    if terms["autonomy"] == "A2":
        transition(proposal, "approved")
    return proposal
