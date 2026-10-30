"""Exact-effect human approvals with immutable, purpose-bound server attestations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.db import connection, connections, transaction

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import JSON, ActionPayload, closed, integer, parse_payload
from apps.ai.action_validation import (
    authority_snapshot,
    database_time,
    validate_current,
)
from apps.ai.attestations import KEY_ID, StepUpEvidence, attest, verify
from apps.ai.models import ActionProposal, Approval
from apps.ai.proposals import transition
from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id

if TYPE_CHECKING:
    from uuid import UUID

MAX_EVIDENCE_AGE_SECONDS: Final = 300


def _verify_evidence(
    evidence: StepUpEvidence,
    *,
    clinic_id: UUID,
    actor: UUID,
    digest: str,
) -> None:
    """Authenticate the current user's exact-effect challenge receipt."""
    if (
        evidence.clinic_id != clinic_id
        or evidence.user_id != actor
        or evidence.digest != digest
    ):
        raise ActionRefusedError(Refusal.ATTESTATION)
    verify(
        purpose="clinic-agent-step-up-v1",
        value=evidence.value(),
        signature=evidence.signature,
    )
    age = database_time().timestamp() - evidence.verified_at
    if not 0 <= age <= MAX_EVIDENCE_AGE_SECONDS:
        raise ActionRefusedError(Refusal.EXPIRED)


def approval_value(approval: Approval) -> dict[str, JSON]:
    """Bind identities, tenant, effect, authority, evidence and key domain."""
    return {
        "id": str(approval.pk),
        "organization_id": str(approval.organization_id),
        "clinic_id": str(approval.clinic_id),
        "proposal_id": str(approval.proposal_id),
        "approver_id": str(approval.approver_id),
        "digest": approval.digest,
        "authority_snapshot": approval.authority_snapshot,
        "step_up_evidence_ref": str(approval.step_up_evidence_ref),
        "step_up_evidence": approval.step_up_evidence,
        "key_id": approval.key_id,
    }


def verify_approvals(
    *, proposal: ActionProposal, payload: ActionPayload, using: str
) -> None:
    """Verify distinct votes, current authority and every attested binding."""
    data = payload.data()
    terms = closed(
        data["approval_policy"], {"policy_id", "version", "quorum", "autonomy"}
    )
    expected = data["approver_authority_snapshots"]
    if not isinstance(expected, list):
        raise ActionRefusedError(Refusal.QUORUM)
    votes = list(
        Approval.objects.using(using).filter(proposal=proposal).order_by("approver_id")
    )
    for vote in votes:
        if vote.digest != payload.digest or vote.authority_snapshot not in expected:
            raise ActionRefusedError(Refusal.DIGEST)
        verify(
            purpose="clinic-agent-approval-v1",
            value=approval_value(vote),
            signature=vote.attestation,
        )
        with connections[using].cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.agent_approval_current(%s,%s)",
                [proposal.pk, vote.pk],
            )
            allowed = cursor.fetchone() == (True,)
        if not allowed:
            raise ActionRefusedError(Refusal.AUTHORITY)
    if len(votes) < integer(terms["quorum"], minimum=0, maximum=2):
        raise ActionRefusedError(Refusal.QUORUM)
    if using == "default" and not any(
        vote.approver_id == current_actor_id() for vote in votes
    ):
        raise ActionRefusedError(Refusal.AUTHORITY)


@transaction.atomic
def approve_action(
    *,
    clinic_id: UUID,
    proposal_id: UUID,
    expected_digest: str,
    evidence: StepUpEvidence,
) -> Approval:
    """Approve exactly the displayed digest, never a substituted latest payload."""
    actor = current_actor_id()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-send-v1',0))"
        )
    proposal = (
        ActionProposal.objects.select_for_update(of=("self",))
        .select_related("run__policy")
        .filter(
            pk=proposal_id,
            clinic_id=clinic_id,
        )
        .first()
    )
    if proposal is None:
        raise ActionAccessDeniedError
    payload = parse_payload(proposal.payload)
    if expected_digest != proposal.digest or payload.digest != expected_digest:
        raise ActionRefusedError(Refusal.DIGEST)
    if proposal.state not in ("validated", "approved"):
        raise ActionRefusedError(Refusal.STATE)
    _verify_evidence(evidence, clinic_id=clinic_id, actor=actor, digest=expected_digest)
    validate_current(
        payload=payload, policy=proposal.run.policy, proposal_id=proposal.pk
    )
    data = payload.data()
    snapshots = data["approver_authority_snapshots"]
    if not isinstance(snapshots, list):
        raise ActionRefusedError(Refusal.QUORUM)
    candidates = [
        item
        for item in snapshots
        if isinstance(item, dict) and item["user_id"] == str(actor)
    ]
    if len(candidates) != 1:
        raise ActionRefusedError(Refusal.AUTHORITY)
    snapshot = candidates[0]
    permission = snapshot["permission"]
    if not isinstance(permission, str):
        raise ActionRefusedError(Refusal.AUTHORITY)
    if snapshot != authority_snapshot(clinic_id=clinic_id, permission=permission):
        raise ActionRefusedError(Refusal.AUTHORITY)
    previous = Approval.objects.filter(proposal=proposal, approver_id=actor).first()
    if previous is not None:
        verify(
            purpose="clinic-agent-approval-v1",
            value=approval_value(previous),
            signature=previous.attestation,
        )
        return previous
    approval = Approval(
        organization_id=proposal.organization_id,
        clinic_id=clinic_id,
        proposal=proposal,
        approver_id=actor,
        digest=expected_digest,
        authority_snapshot=snapshot,
        step_up_evidence_ref=evidence.evidence_id,
        step_up_evidence={"facts": evidence.value(), "signature": evidence.signature},
        key_id=KEY_ID,
    )
    approval.attestation = attest(
        purpose="clinic-agent-approval-v1", value=approval_value(approval)
    )
    approval.save()
    record_phase1_event(
        "ai.approval.recorded", clinic_id=clinic_id, affected_record_id=approval.pk
    )
    policy = closed(
        data["approval_policy"], {"policy_id", "version", "quorum", "autonomy"}
    )
    if (
        Approval.objects.filter(proposal=proposal).count()
        >= integer(policy["quorum"], minimum=0, maximum=2)
        and proposal.state == "validated"
    ):
        transition(proposal, "approved")
    return approval


@transaction.atomic
def reject_action(*, clinic_id: UUID, proposal_id: UUID, expected_digest: str) -> None:
    """Reject only the inspected effect; do not redirect a stale decision."""
    proposal = (
        ActionProposal.objects.select_for_update()
        .filter(pk=proposal_id, clinic_id=clinic_id)
        .first()
    )
    if proposal is None:
        raise ActionAccessDeniedError
    if expected_digest != proposal.digest:
        raise ActionRefusedError(Refusal.DIGEST)
    if proposal.state not in ("proposal", "validated", "approved"):
        raise ActionRefusedError(Refusal.STATE)
    transition(proposal, "rejected", reason=Refusal.STATE.value)
