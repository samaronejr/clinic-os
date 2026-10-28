"""Worst-case model outputs hit real parsers, policies, approvals and domain tools."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.ai.action_contracts import ActionInputError, ActionRefusedError, Refusal
from apps.ai.action_payload import parse_payload
from apps.ai.execution import execute_action
from apps.ai.models import ActionExecution, ActionReceipt, AgentRun, Approval
from apps.ai.proposals import AgentContext
from apps.comms import agent_reminders
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from apps.workflows.services import create_task

from ai.agent_support import AgentWorld, agent_world, approve, propose
from ai.security.corpus import (
    CASES,
    VARIANTS,
    AttackCase,
    InjectionFollowingModel,
    Variant,
)
from ai.security.support import CAPABILITIES, security_agent
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


@pytest.fixture(params=CAPABILITIES)
def world(
    request: pytest.FixtureRequest,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> AgentWorld:
    return security_agent(
        agent_world(rbac_graph, monkeypatch, synthetic_secret_backend),
        capability=request.param,
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
@pytest.mark.parametrize("variant", VARIANTS)
def test_injection_following_model_cannot_acquire_an_effect(
    world: AgentWorld, case: AttackCase, variant: Variant
) -> None:
    source = case.source(variant)
    fake = InjectionFollowingModel([])
    response = fake.complete(case, source=source, seed=world.payload)
    assert fake.inputs == [source.model_data()]
    assert json.loads(response) != world.payload.data()
    # Keep observations at the actual domain boundary, not a no-op tool mock.
    with (
        patch(
            "apps.ai.tool_gateway.agent_reminders.send_reminder",
            wraps=agent_reminders.send_reminder,
        ) as external,
        patch(
            "apps.ai.tool_gateway.create_task",
            wraps=create_task,
        ) as draft,
    ):
        try:
            payload = parse_payload(response)
        except ActionInputError:
            proposal = None
        else:
            attacked = replace(world, payload=payload)
            proposal = propose(attacked, context=AgentContext(sources=(source,)))
            assert proposal.state == "rejected"
            assert proposal.reason_code in {
                Refusal.INJECTION.value,
                Refusal.TOOL.value,
            }
            result = execute_action(
                clinic_id=world.gateway.graph.clinic_a,
                proposal_id=proposal.pk,
                principal_id=world.principal.pk,
            )
            assert result.receipt_id is None
        external.assert_not_called()
        draft.assert_not_called()
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        assert not Approval.objects.exists()
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()
        assert not Task.objects.exists()
        if proposal is None:
            assert not AgentRun.objects.exists()


def test_encoded_injection_still_fails_without_the_keyword_detector(
    world: AgentWorld,
) -> None:
    case = next(case for case in CASES if case.identifier == "email-en")
    source = case.source("base64")
    assert not source.flagged
    response = InjectionFollowingModel([]).complete(
        case, source=source, seed=world.payload
    )
    proposal = propose(
        replace(world, payload=parse_payload(response)),
        context=AgentContext(sources=(source,)),
    )
    assert (proposal.state, proposal.reason_code) == ("rejected", Refusal.TOOL.value)


def test_untrusted_text_cannot_supply_the_missing_human_approval(
    world: AgentWorld,
) -> None:
    source = next(case for case in CASES if case.identifier == "pdf-en").source(
        "base64"
    )
    assert not source.flagged
    proposal = propose(world, context=AgentContext(sources=(source,)))
    assert proposal.state == "validated"
    result = execute_action(
        clinic_id=world.gateway.graph.clinic_a,
        proposal_id=proposal.pk,
        principal_id=world.principal.pk,
    )
    assert result.reason is Refusal.STATE
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert not Approval.objects.exists()
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()


def test_changed_model_output_cannot_reuse_a_previous_approval(
    world: AgentWorld,
) -> None:
    proposal = propose(world)
    approve(world, proposal)
    data = world.payload.data()
    data["patient_id"] = str(uuid4())
    # The model cannot replace the bytes behind an existing idempotency key.
    with pytest.raises(ActionRefusedError) as refused:
        propose(replace(world, payload=parse_payload(json.dumps(data))))
    assert refused.value.reason is Refusal.DIGEST
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert Approval.objects.filter(proposal=proposal).count() == 1
        assert not ActionExecution.objects.exists()


@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("autonomy", ["A1", "A2", "A3"])
def test_positive_control_each_supported_capability_and_autonomy_can_execute(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    capability: str,
    autonomy: str,
) -> None:
    subject = security_agent(
        agent_world(
            rbac_graph, monkeypatch, synthetic_secret_backend, autonomy=autonomy
        ),
        capability=capability,
    )
    proposal = propose(subject)
    if autonomy != "A2":
        approve(subject, proposal)
    if autonomy == "A1":
        with (
            runtime_role(),
            tenant_context(subject.gateway.administrator, rbac_graph.organization_a),
        ):
            result = execute_action(
                clinic_id=rbac_graph.clinic_a, proposal_id=proposal.pk
            )
    else:
        result = execute_action(
            clinic_id=rbac_graph.clinic_a,
            proposal_id=proposal.pk,
            principal_id=subject.principal.pk,
        )
    assert result.state == "receipt"
    assert result.receipt_id is not None
