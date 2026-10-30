"""Protected proposal loading and append-only, server-attested receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connections

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionInputError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import JSON, ActionPayload, parse_payload
from apps.ai.attestations import KEY_ID, attest
from apps.ai.models import (
    ActionExecution,
    ActionProposal,
    ActionReceipt,
    AgentPolicyVersion,
)
from apps.audit.services import record_phase1_event
from apps.core.secrets import secret_store

if TYPE_CHECKING:
    from uuid import UUID


def load_action(
    *, clinic_id: UUID, proposal_id: UUID, using: str
) -> tuple[ActionProposal, ActionPayload, AgentPolicyVersion]:
    """Reload immutable ciphertext without exposing other runs to the machine role."""
    proposal = (
        ActionProposal.objects.using(using)
        .defer("payload")
        .select_for_update()
        .filter(
            pk=proposal_id,
            clinic_id=clinic_id,
        )
        .first()
    )
    if proposal is None:
        raise ActionAccessDeniedError
    if using == "agent":
        with connections[using].cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.agent_action_payload(%s,%s)",
                [proposal.pk, secret_store().get_secret("tenant-kek")],
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[0], str):
            raise ActionAccessDeniedError
        raw = row[0]
    else:
        raw = proposal.payload
    try:
        payload = parse_payload(raw)
    except ActionInputError:
        raise ActionRefusedError(Refusal.DIGEST) from None
    if payload.digest != proposal.digest:
        raise ActionRefusedError(Refusal.DIGEST)
    policy = (
        AgentPolicyVersion.objects.using(using)
        .filter(agentrun__id=proposal.run_id)
        .first()
    )
    if policy is None:
        raise ActionAccessDeniedError
    return proposal, payload, policy


def receipt_value(receipt: ActionReceipt) -> dict[str, JSON]:
    """Bind the receipt to the exact effect, scope, fence and outcome."""
    return {
        "id": str(receipt.pk),
        "organization_id": str(receipt.organization_id),
        "clinic_id": str(receipt.clinic_id),
        "execution_id": str(receipt.execution_id),
        "kind": receipt.kind,
        "outcome": receipt.outcome,
        "provider_reference": str(receipt.provider_reference),
        "digest": receipt.digest,
        "fencing_token": receipt.fencing_token,
        "key_id": receipt.key_id,
    }


def record_receipt(
    *, execution: ActionExecution, kind: str, outcome: str, reference: UUID, using: str
) -> ActionReceipt:
    """Record authenticated outcomes once; never imply a qualified signature."""
    receipt = ActionReceipt(
        organization_id=execution.organization_id,
        clinic_id=execution.clinic_id,
        execution=execution,
        kind=kind,
        outcome=outcome,
        provider_reference=reference,
        digest=execution.digest,
        fencing_token=execution.fencing_token,
        key_id=KEY_ID,
    )
    receipt.attestation = attest(
        purpose="clinic-agent-receipt-v1", value=receipt_value(receipt)
    )
    receipt.save(using=using)
    record_phase1_event(
        "ai.receipt.recorded",
        clinic_id=execution.clinic_id,
        affected_record_id=receipt.pk,
    )
    return receipt
