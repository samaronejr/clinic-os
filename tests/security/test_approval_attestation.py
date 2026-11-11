"""AA-S2: a real agent approval stores its authority, step-up and keyed attestation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.action_validation import authority_snapshot
from apps.ai.approvals import approval_value
from apps.ai.attestations import KEY_ID, verify
from apps.ai.models import Approval
from apps.tenancy.db import tenant_context

from ai.agent_support import agent_world, approve, propose
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
PURPOSE = "clinic-agent-approval-v1"
COLUMNS = (
    "id",
    "organization_id",
    "clinic_id",
    "proposal_id",
    "approver_id",
    "digest",
    "authority_snapshot",
    "step_up_evidence_ref",
    "step_up_evidence",
    "key_id",
    "attestation",
    "step_up_verified_at",
)


def test_approval_stores_authority_snapshot_step_up_ref_and_keyed_attestation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    superuser_database_url: str,
) -> None:
    # Given a validated proposal approved after a real persisted step-up.
    world = agent_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    proposal = propose(world)
    approve(world, proposal)
    administrator = world.gateway.administrator
    with runtime_role(), tenant_context(administrator, rbac_graph.organization_a):
        stored = Approval.objects.get(proposal=proposal)
        permission = stored.authority_snapshot["permission"]
        assert isinstance(permission, str)
        current = authority_snapshot(
            clinic_id=rbac_graph.clinic_a, permission=permission
        )
    # Then the row keeps the approver's current authority, the step-up receipt
    # it was bound to and the instant PostgreSQL saw the verified session ...
    assert stored.approver_id == administrator
    assert stored.digest == proposal.digest
    assert stored.authority_snapshot == current
    assert stored.step_up_evidence_ref is not None
    assert stored.step_up_evidence["facts"]["id"] == str(stored.step_up_evidence_ref)
    assert stored.step_up_verified_at is not None
    # ... and an HMAC under the dedicated key that verifies independently.
    assert stored.key_id == KEY_ID
    verify(purpose=PURPOSE, value=approval_value(stored), signature=stored.attestation)
    # A changed snapshot or another purpose no longer verifies.
    stored.authority_snapshot = {**stored.authority_snapshot, "permission": "x"}
    with pytest.raises(ActionRefusedError) as tampered:
        verify(
            purpose=PURPOSE, value=approval_value(stored), signature=stored.attestation
        )
    assert tampered.value.reason is Refusal.ATTESTATION
    stored.authority_snapshot = current
    with pytest.raises(ActionRefusedError):
        verify(
            purpose="clinic-agent-step-up-v1",
            value=approval_value(stored),
            signature=stored.attestation,
        )
