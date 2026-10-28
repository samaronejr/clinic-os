"""Exercise every currently published agent capability under actual authority."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from apps.ai.action_payload import parse_payload
from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.policy import OrganizationPolicy
from apps.identity.clinic_configuration import latest_configuration
from apps.tenancy.db import tenant_context

from ai.support import model
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from ai.agent_support import AgentWorld

CAPABILITIES = ("front_desk", "analyst")


def security_agent(
    world: AgentWorld,
    *,
    capability: str,
    budget: int = 20,
) -> AgentWorld:
    graph = world.gateway.graph
    autonomy = world.policy.autonomy_ceiling
    if capability == "analyst":
        model(purpose=Purpose.ANALYST)
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        configuration = latest_configuration(graph.clinic_a)
        assert configuration is not None
        set_organization_policy(
            clinic_id=graph.clinic_a,
            expected_version=configuration.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=CAPABILITIES,
                monthly_budget_micros=10_000_000,
                purpose_budgets=tuple(
                    (purpose, 10_000_000) for purpose in CAPABILITIES
                ),
            ),
        )
        policy = publish_agent_policy(
            clinic_id=graph.clinic_a,
            expected_version=1 if capability == "front_desk" else 0,
            terms=PolicyTerms(
                capability,
                autonomy,
                ("workflows.review_task",)
                if autonomy == "A1"
                else ("comms.synthetic_reminder",),
                budget_actions=budget,
                budget_micros=budget,
                enabled=True,
                principal_id=world.principal.pk if autonomy != "A1" else None,
                compensation="synthetic_cancel",
            ),
        )
    data = world.payload.data()
    approval = data["approval_policy"]
    assert isinstance(approval, dict)
    approval.update(policy_id=str(policy.pk), version=policy.version)
    return replace(world, policy=policy, payload=parse_payload(json.dumps(data)))
