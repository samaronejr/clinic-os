"""Real-role action fixtures: actual policy, workflow publication and OTP evidence."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ai.action_payload import ActionPayload, parse_payload
from apps.ai.action_validation import authority_snapshot, database_time, source_snapshot
from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.approvals import approve_action
from apps.ai.attestations import step_up_evidence
from apps.ai.proposals import AgentContext, propose_action
from apps.identity.models import ServicePrincipal, ServicePrincipalGrant, UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from apps.tenancy.db import tenant_context
from apps.workflows.services import publish_definition
from django.db import connections

from ai.support import GatewayWorld, seed
from ai.test_action_payload import payload_json
from auth.stepup_test_support import verified_request
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    import pytest
    from apps.ai.models import ActionProposal, AgentPolicyVersion, Approval
    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class AgentWorld:
    gateway: GatewayWorld
    principal: ServicePrincipal
    policy: AgentPolicyVersion
    source_id: UUID
    payload: ActionPayload
    request: HttpRequest


def agent_world(
    graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    secret_root: Path,
    *,
    autonomy: str = "A3",
    compensation: str = "synthetic_cancel",
) -> AgentWorld:
    secret = secret_root / "agent-attestation-v1.secret"
    secret.write_text(secrets.token_hex(32))
    secret.chmod(0o600)
    gateway = seed(graph, monkeypatch)
    request = verified_request(gateway.administrator)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(user_id=gateway.administrator).update(
            workflow_bundle_version=2
        )
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name="synthetic-agent",
            db_identity=str(connections["agent"].settings_dict["USER"]),
            purpose="agent-execution",
            grant_set_version=2,
        )
        ServicePrincipalGrant.objects.create(
            organization_id=graph.organization_a,
            principal=principal,
            permission="agent.execute",
            grant_set_version=2,
        )
    version = propose_version(
        "email",
        version_input=VersionInput(
            provider="synthetic",
            account=f"synthetic-{uuid4().hex}",
            environment="synthetic",
            api_version="v1",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    with runtime_role(), tenant_context(gateway.administrator, graph.organization_a):
        operation = (
            "workflows.review_task" if autonomy == "A1" else "comms.synthetic_reminder"
        )
        policy = publish_agent_policy(
            clinic_id=graph.clinic_a,
            expected_version=0,
            terms=PolicyTerms(
                "front_desk",
                autonomy,
                (operation,),
                enabled=True,
                principal_id=principal.pk if autonomy != "A1" else None,
                compensation=compensation,
            ),
        )
        source = publish_definition(
            clinic_id=graph.clinic_a,
            key="synthetic-agent-task",
            steps=[{"handler": "timer", "seconds": 1}],
            idempotency_key=uuid4(),
        )
        snapshot = authority_snapshot(
            clinic_id=graph.clinic_a,
            permission="tasks.assign"
            if autonomy == "A1"
            else "automation.organization",
        )
        now = database_time()
        data = payload_json()
        data.update(
            {
                "action_id": str(uuid4()),
                "organization_id": str(graph.organization_a),
                "clinic_id": str(graph.clinic_a),
                "idempotency_key": str(uuid4()),
                "operation": {"name": operation, "version": 1},
                "subject": {
                    "kind": "workflow_definition",
                    "id": str(source.pk),
                    "expected_state": "published",
                    "revision": source.revision,
                },
                "source_versions": [
                    source_snapshot(clinic_id=graph.clinic_a, source_id=source.pk)
                ],
                "provider": {
                    "capability_version": str(version.pk),
                    "account": version.account,
                    "environment": "synthetic",
                    "api_version": "v1",
                },
                "not_before": (now - timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "expires_at": (now + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "approver_authority_snapshots": [] if autonomy == "A2" else [snapshot],
                "approval_policy": {
                    "policy_id": str(policy.pk),
                    "version": 1,
                    "quorum": 0 if autonomy == "A2" else 1,
                    "autonomy": autonomy,
                },
                "compensation_policy": {"mode": compensation, "version": 1},
            }
        )
        if autonomy == "A1":
            data["parameters"] = {
                "due_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
            }
    return AgentWorld(
        gateway, principal, policy, source.pk, parse_payload(json.dumps(data)), request
    )


def propose(
    world: AgentWorld, *, context: AgentContext | None = None
) -> ActionProposal:
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        return propose_action(
            clinic_id=graph.clinic_a,
            payload=world.payload,
            context=context if context is not None else AgentContext(),
        )


def approve(world: AgentWorld, proposal: ActionProposal) -> Approval:
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        evidence = step_up_evidence(
            world.request,
            clinic_id=graph.clinic_a,
            digest=proposal.digest,
        )
        return approve_action(
            clinic_id=graph.clinic_a,
            proposal_id=proposal.pk,
            expected_digest=proposal.digest,
            evidence=evidence,
        )
