"""Neither hostile request ceilings nor new action identities expand the budget."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.ai.action_contracts import Refusal
from apps.ai.action_payload import parse_payload
from apps.ai.action_validation import database_time
from apps.ai.artifacts import create_input
from apps.ai.contracts import Budget, Purpose
from apps.ai.execution import execute_action
from apps.ai.models import ActionExecution, ActionReceipt, AIInvocation
from apps.ai.services import invoke
from apps.comms import agent_reminders
from apps.comms.models import IntegrationOperation
from apps.identity.current_context import require_permission
from apps.tenancy.db import tenant_context

from ai.agent_support import agent_world, approve, propose
from ai.security.corpus import CASES
from ai.security.support import CAPABILITIES, security_agent
from ai.support import DEFAULT_BUDGET, seed, set_policy
from identity.test_service_principals import agent_password
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

__all__ = ("agent_password",)
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


@pytest.fixture(autouse=True)
def machine_login(agent_password: None) -> None:
    pass


def test_hostile_ceiling_and_repeated_prompts_cannot_override_organization_cap(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = seed(rbac_graph, monkeypatch)
    set_policy(world, cap=1)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        for case in CASES:
            source = create_input(
                clinic_id=rbac_graph.clinic_a,
                purpose=Purpose.FRONT_DESK,
                content=case.source("base64").text,
            )
            result = invoke(
                clinic_id=rbac_graph.clinic_a,
                purpose=Purpose.FRONT_DESK,
                principal=rbac_graph.shared_user,
                inputs_ref=source.pk,
                budget=Budget(DEFAULT_BUDGET.limits, 10**15),
            )
            assert result.reason == "budget_exhausted"
            assert result.invocation_id is result.operation_id is None
        assert not AIInvocation.objects.exists()
        assert not IntegrationOperation.objects.exists()
        assert (
            require_permission("appointment.read", clinic_id=rbac_graph.clinic_a)
            == rbac_graph.shared_user
        )
    assert world.publications == []


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_fresh_action_identities_cannot_reexecute_after_budget_exhaustion(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    capability: str,
) -> None:
    world = security_agent(
        agent_world(rbac_graph, monkeypatch, synthetic_secret_backend),
        capability=capability,
        budget=1,
    )
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, rbac_graph.organization_a),
    ):
        now = database_time()
    monkeypatch.setattr("apps.ai.execution.database_time", lambda **_kwargs: now)
    monkeypatch.setattr(
        "apps.ai.action_validation.database_time", lambda **_kwargs: now
    )
    with patch(
        "apps.ai.tool_gateway.agent_reminders.send_reminder",
        wraps=agent_reminders.send_reminder,
    ) as external:
        first = propose(world)
        approve(world, first)
        completed = execute_action(
            clinic_id=rbac_graph.clinic_a,
            proposal_id=first.pk,
            principal_id=world.principal.pk,
        )
        assert completed.state == "receipt"
        for _index in range(3):
            data = world.payload.data()
            data.update(action_id=str(uuid4()), idempotency_key=str(uuid4()))
            next_world = replace(world, payload=parse_payload(json.dumps(data)))
            proposal = propose(next_world)
            approve(next_world, proposal)
            refused = execute_action(
                clinic_id=rbac_graph.clinic_a,
                proposal_id=proposal.pk,
                principal_id=world.principal.pk,
            )
            assert refused.reason is Refusal.BUDGET
            assert refused.execution_id is refused.receipt_id is None
        assert external.call_count == 1
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, rbac_graph.organization_a),
    ):
        assert ActionExecution.objects.count() == ActionReceipt.objects.count() == 1
