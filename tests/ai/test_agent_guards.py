"""Executed approval, scope, recovery and native refusal boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.ai import action_validation, agent_views, execution, tasks
from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import parse_payload
from apps.ai.action_recovery import compensate_action, reconcile_action
from apps.ai.action_runtime import action_scope
from apps.ai.action_validation import source_snapshot
from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.approvals import approval_value, approve_action, reject_action
from apps.ai.attestations import attest, step_up_evidence
from apps.ai.models import (
    ActionExecution,
    ActionProposal,
    ActionReceipt,
    AgentRun,
    Approval,
    Compensation,
)
from apps.ai.proposals import AgentContext
from apps.ai.tasks import run_agent
from apps.comms import agent_reminders
from apps.core.fairness import DEFER_COUNTDOWN_SECONDS
from apps.tenancy.db import ServicePrincipalAccessDeniedError, tenant_context
from apps.workflows.services import publish_definition
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory
from django.urls import resolve, reverse

from ai.agent_support import AgentWorld, agent_world, approve, propose
from ai.test_agent_execution import agent_password, execute, machine_login, world
from auth.stepup_test_support import verified_request
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from apps.ai.action_payload import ActionPayload
    from apps.ai.models import AgentPolicyVersion
    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph

__all__ = ("agent_password", "machine_login", "world")
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


def request_for(
    actor: UUID,
    clinic: UUID,
    route: str,
    data: dict[str, str] | None = None,
) -> HttpRequest:
    verified = verified_request(actor)
    path = reverse(route, args=(clinic,))
    request = (
        RequestFactory().get(path)
        if data is None
        else RequestFactory().post(path, data)
    )
    request.user, request.session = verified.user, verified.session
    request.resolver_match = resolve(path)
    request.session.modified = False
    return request


def test_agent_scope_and_approval_refusals_are_side_effect_free(
    world: AgentWorld,
) -> None:
    graph = world.gateway.graph
    proposal = propose(world)
    with pytest.raises(ActionAccessDeniedError):
        run_agent(
            clinic_id=str(graph.clinic_a),
            proposal_id=str(uuid4()),
            principal_id=str(world.principal.pk),
        )
    with (
        pytest.raises(ActionAccessDeniedError),
        action_scope(clinic_id=graph.clinic_a, principal_id=None),
    ):
        pytest.fail("human execution escaped its request transaction")
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        revision = source_snapshot(clinic_id=graph.clinic_a, source_id=world.source_id)[
            "revision"
        ]
        assert isinstance(revision, int)
        assert revision >= 1
        evidence = step_up_evidence(
            world.request, clinic_id=graph.clinic_a, digest=proposal.digest
        )
        for clinic, identifier in (
            (graph.clinic_a, uuid4()),
            (graph.clinic_b, proposal.pk),
        ):
            with pytest.raises(ActionAccessDeniedError):
                approve_action(
                    clinic_id=clinic,
                    proposal_id=identifier,
                    expected_digest=proposal.digest,
                    evidence=evidence,
                )
            with pytest.raises(ActionAccessDeniedError):
                reject_action(
                    clinic_id=clinic,
                    proposal_id=identifier,
                    expected_digest=proposal.digest,
                )
            with pytest.raises(ActionAccessDeniedError):
                source_snapshot(clinic_id=clinic, source_id=identifier)
        for value in ("0" * 64, "f" * 64):
            with pytest.raises(ActionRefusedError) as denied:
                approve_action(
                    clinic_id=graph.clinic_a,
                    proposal_id=proposal.pk,
                    expected_digest=value,
                    evidence=evidence,
                )
            assert denied.value.reason is Refusal.DIGEST
        assert not Approval.objects.filter(proposal=proposal).exists()
        assert not ActionExecution.objects.filter(proposal=proposal).exists()
    for identifier in (proposal.pk, uuid4()):
        with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
            assert not ActionProposal.objects.filter(pk=identifier).exists()
        with pytest.raises(ServicePrincipalAccessDeniedError):
            execution.execute_action(
                clinic_id=graph.clinic_c,
                proposal_id=identifier,
                principal_id=world.principal.pk,
            )


def test_proposal_replay_requires_the_same_canonical_digest(world: AgentWorld) -> None:
    proposal = propose(world)
    assert propose(world).pk == proposal.pk
    data = world.payload.data()
    data["action_id"] = str(uuid4())
    changed = replace(world, payload=parse_payload(json.dumps(data)))
    assert changed.payload.digest != proposal.digest

    with pytest.raises(ActionRefusedError) as refused:
        propose(changed)
    assert refused.value.reason is Refusal.DIGEST
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert ActionProposal.objects.count() == AgentRun.objects.count() == 1
        assert ActionProposal.objects.get(pk=proposal.pk).state == "validated"
        assert not Approval.objects.exists()
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()


@pytest.mark.parametrize(
    "context",
    [
        AgentContext(trigger="document"),
        AgentContext(evaluation_set="unreviewed-agent"),
    ],
    ids=["untrusted-trigger", "unreviewed-evaluation"],
)
def test_proposal_refuses_untrusted_execution_context_before_a_run_exists(
    world: AgentWorld, context: AgentContext
) -> None:
    with pytest.raises(ActionRefusedError) as refused:
        propose(world, context=context)
    assert refused.value.reason is Refusal.POLICY
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert not AgentRun.objects.exists()
        assert not ActionProposal.objects.exists()
        assert not Approval.objects.exists()
        assert not ActionExecution.objects.exists()


def test_proposal_refuses_a_policy_that_does_not_exist_in_its_clinic(
    world: AgentWorld,
) -> None:
    data = world.payload.data()
    policy = data["approval_policy"]
    assert isinstance(policy, dict)
    policy["policy_id"] = str(uuid4())
    changed = replace(world, payload=parse_payload(json.dumps(data)))
    with pytest.raises(ActionAccessDeniedError):
        propose(changed)
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert not AgentRun.objects.exists()
        assert not ActionProposal.objects.exists()


def test_proposal_preserves_a_source_refusal_without_creating_approval_or_effect(
    world: AgentWorld,
) -> None:
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        publish_definition(
            clinic_id=graph.clinic_a,
            key="synthetic-agent-task",
            steps=[{"handler": "timer", "seconds": 2}],
            idempotency_key=uuid4(),
        )
    proposal = propose(world)
    assert (proposal.state, proposal.reason_code) == ("rejected", Refusal.SOURCE.value)
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        assert AgentRun.objects.get(pk=proposal.run_id).outcome == "rejected"
        assert not Approval.objects.exists()
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()


def test_approval_refuses_a_payload_that_no_longer_matches_its_displayed_digest(
    world: AgentWorld,
) -> None:
    proposal = propose(world)
    data = world.payload.data()
    data["idempotency_key"] = str(uuid4())
    changed = parse_payload(json.dumps(data))
    assert changed.digest != proposal.digest
    with (
        owner_context(world.gateway.graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "ALTER TABLE clinic_app.ai_actionproposal "
            "DISABLE TRIGGER agent_record_guard"
        )
        try:
            ActionProposal.objects.filter(pk=proposal.pk).update(
                payload=changed.canonical.decode()
            )
        finally:
            cursor.execute(
                "ALTER TABLE clinic_app.ai_actionproposal "
                "ENABLE TRIGGER agent_record_guard"
            )

    with (
        patch.object(
            agent_reminders, "send_reminder", wraps=agent_reminders.send_reminder
        ) as provider,
        pytest.raises(ActionRefusedError) as refused,
    ):
        approve(world, proposal)
    assert refused.value.reason is Refusal.DIGEST
    provider.assert_not_called()
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert ActionProposal.objects.get(pk=proposal.pk).state == "validated"
        assert not Approval.objects.exists()
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()


def test_execution_refuses_an_approval_bound_to_another_digest(
    world: AgentWorld,
) -> None:
    proposal = propose(world)
    vote = approve(world, proposal)
    data = world.payload.data()
    data["idempotency_key"] = str(uuid4())
    changed = parse_payload(json.dumps(data))
    assert changed.digest != proposal.digest
    with (
        owner_context(world.gateway.graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "ALTER TABLE clinic_app.ai_approval DISABLE TRIGGER agent_record_guard"
        )
        try:
            Approval.objects.filter(pk=vote.pk).update(digest=changed.digest)
        finally:
            cursor.execute(
                "ALTER TABLE clinic_app.ai_approval ENABLE TRIGGER agent_record_guard"
            )
    with patch.object(
        agent_reminders, "send_reminder", wraps=agent_reminders.send_reminder
    ) as provider:
        result = execute(world, proposal.pk)
    assert result.reason is Refusal.DIGEST
    assert result.receipt_id is None
    provider.assert_not_called()
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert not ActionReceipt.objects.exists()


@pytest.mark.parametrize("attack", ["signature", "purpose", "key", "proposal"])
def test_approval_attestation_replay_and_key_confusion_are_rejected(
    world: AgentWorld,
    attack: str,
) -> None:
    proposal = propose(world)
    vote = approve(world, proposal)
    if attack == "signature":
        vote.attestation = "0" * 64
    elif attack == "purpose":
        vote.attestation = attest(
            purpose="clinic-agent-receipt-v1", value=approval_value(vote)
        )
    elif attack == "key":
        vote.key_id = "unregistered-attestation-key"
        vote.attestation = attest(
            purpose="clinic-agent-approval-v1", value=approval_value(vote)
        )
    else:
        value = {**approval_value(vote), "proposal_id": str(uuid4())}
        vote.attestation = attest(purpose="clinic-agent-approval-v1", value=value)
    with (
        owner_context(world.gateway.graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "ALTER TABLE clinic_app.ai_approval DISABLE TRIGGER agent_record_guard"
        )
        try:
            Approval.objects.filter(pk=vote.pk).update(
                attestation=vote.attestation, key_id=vote.key_id
            )
        finally:
            cursor.execute(
                "ALTER TABLE clinic_app.ai_approval ENABLE TRIGGER agent_record_guard"
            )
    result = execute(world, proposal.pk)
    assert result.reason is Refusal.ATTESTATION
    assert result.receipt_id is None


def test_fabricated_step_up_and_double_approval_cannot_add_votes(
    world: AgentWorld,
) -> None:
    proposal = propose(world)
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        evidence = step_up_evidence(
            world.request, clinic_id=graph.clinic_a, digest=proposal.digest
        )
        for forged in (
            replace(evidence, signature="0" * 64),
            replace(evidence, clinic_id=graph.clinic_b),
            replace(evidence, user_id=uuid4()),
            replace(evidence, digest="f" * 64),
        ):
            with pytest.raises(ActionRefusedError) as denied:
                approve_action(
                    clinic_id=graph.clinic_a,
                    proposal_id=proposal.pk,
                    expected_digest=proposal.digest,
                    evidence=forged,
                )
            assert denied.value.reason is Refusal.ATTESTATION
    first = approve(world, proposal)
    second = approve(world, proposal)
    assert second.pk == first.pk
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        assert Approval.objects.filter(proposal=proposal).count() == 1
        with pytest.raises(DatabaseError), transaction.atomic():
            Approval.objects.filter(pk=first.pk).update(digest="f" * 64)
    assert execute(world, proposal.pk).state == "receipt"


def test_successor_disabled_policy_revokes_queued_approval(world: AgentWorld) -> None:
    proposal = propose(world)
    approve(world, proposal)
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        publish_agent_policy(
            clinic_id=graph.clinic_a,
            expected_version=1,
            terms=PolicyTerms(
                "front_desk",
                "A3",
                ("comms.synthetic_reminder",),
                principal_id=world.principal.pk,
            ),
        )
    assert execute(world, proposal.pk).reason is Refusal.POLICY


def test_bounded_policy_executes_without_fabricating_human_approval(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    subject = agent_world(
        rbac_graph,
        monkeypatch,
        synthetic_secret_backend,
        autonomy="A2",
    )
    proposal = propose(subject)
    assert proposal.state == "approved"
    assert execute(subject, proposal.pk).state == "receipt"
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, rbac_graph.organization_a),
    ):
        assert not Approval.objects.filter(proposal=proposal).exists()
        assert ActionReceipt.objects.filter(execution__proposal=proposal).count() == 1


def test_send_boundary_revalidation_prevents_dispatch(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = propose(world)
    approve(world, proposal)
    original = action_validation.validate_current
    checks: list[UUID] = []

    def revalidate(
        *,
        payload: ActionPayload,
        policy: AgentPolicyVersion,
        proposal_id: UUID,
        using: str = "default",
    ) -> None:
        checks.append(proposal_id)
        if len(checks) == 2:
            raise ActionRefusedError(Refusal.AUTHORITY)
        original(payload=payload, policy=policy, proposal_id=proposal_id, using=using)

    monkeypatch.setattr(execution, "validate_current", revalidate)
    result = execute(world, proposal.pk)
    assert result.reason is Refusal.AUTHORITY
    assert len(checks) == 2
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        intent = ActionExecution.objects.get(proposal=proposal)
        assert intent.state == "rejected"
        assert intent.send_started_at is None
        assert not ActionReceipt.objects.filter(execution=intent).exists()


def test_unknown_outcome_and_failed_compensation_require_manual_follow_up(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = propose(world)
    approve(world, proposal)
    original = agent_reminders.send_reminder

    def failed(*, provider_key: UUID, template: str) -> agent_reminders.ReminderOutcome:
        original(provider_key=provider_key, template=template)
        return agent_reminders._outcome(provider_key, "failed")

    attempts: list[tuple[UUID, UUID]] = []

    def uncertain(
        *, provider_key: UUID, original_key: UUID
    ) -> agent_reminders.ReminderOutcome:
        attempts.append((provider_key, original_key))
        raise TimeoutError

    monkeypatch.setattr(agent_reminders, "send_reminder", failed)
    monkeypatch.setattr(agent_reminders, "cancel_reminder", uncertain)
    assert execute(world, proposal.pk).state == "failure"
    for _ in range(2):
        result = compensate_action(
            clinic_id=world.gateway.graph.clinic_a,
            proposal_id=proposal.pk,
            principal_id=world.principal.pk,
        )
        assert result.state == "manual_required"
    assert len(attempts) == 1
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, world.gateway.graph.organization_a),
    ):
        assert Compensation.objects.filter(execution__proposal=proposal).count() == 1
    with pytest.raises(ActionRefusedError) as denied:
        reconcile_action(
            clinic_id=world.gateway.graph.clinic_a,
            proposal_id=proposal.pk,
            principal_id=world.principal.pk,
        )
    assert denied.value.reason is Refusal.STATE


def test_native_agent_decisions_use_the_displayed_digest_and_preserve_refusals(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    subject = agent_world(
        rbac_graph,
        monkeypatch,
        synthetic_secret_backend,
        autonomy="A1",
    )
    proposal = propose(subject)
    graph = subject.gateway.graph
    valid = {
        "proposal_id": str(proposal.pk),
        "digest": proposal.digest,
        "command": "approve",
    }
    for data, status in (
        (None, 200),
        ({**valid, "unexpected": "synthetic"}, 400),
        ({**valid, "digest": "f" * 64}, 409),
        ({**valid, "proposal_id": str(uuid4())}, 403),
        (valid, 302),
    ):
        request = request_for(
            subject.gateway.administrator, graph.clinic_a, "ai:agents", data
        )
        with (
            runtime_role(),
            tenant_context(subject.gateway.administrator, graph.organization_a),
        ):
            response = agent_views.agent_queue(request, graph.clinic_a)
        assert response.status_code == status
        if status == 403:
            assert not response.cookies
            assert request.session.modified is False
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, graph.organization_a),
    ):
        assert ActionProposal.objects.get(pk=proposal.pk).state == "receipt"
        assert ActionReceipt.objects.filter(execution__proposal=proposal).count() == 1
    for route, view in (
        ("ai:agents", agent_views.agent_queue),
        ("ai:agent-settings", agent_views.agent_settings),
    ):
        for clinic in (graph.clinic_c, uuid4()):
            request = request_for(subject.gateway.administrator, clinic, route)
            with (
                runtime_role(),
                tenant_context(subject.gateway.administrator, graph.organization_a),
            ):
                response = view(request, clinic)
            assert response.status_code == 403
            assert not response.cookies
            assert request.session.modified is False
    request = request_for(
        subject.gateway.administrator, graph.clinic_a, "ai:agent-settings"
    )
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, graph.organization_a),
    ):
        assert agent_views.agent_settings(request, graph.clinic_a).status_code == 200


def test_fairness_defers_only_the_exact_agent_task_and_tenant(
    world: AgentWorld,
) -> None:
    # Given an approved action whose tenant cannot acquire a fairness token.
    proposal = propose(world)
    approve(world, proposal)
    graph = world.gateway.graph
    references = {
        "clinic_id": str(graph.clinic_a),
        "proposal_id": str(proposal.pk),
        "principal_id": str(world.principal.pk),
    }
    with (
        patch.object(tasks, "fair_acquire", return_value=False) as fairness,
        patch.object(tasks.execute_agent, "apply_async") as agent_sender,
        patch.object(tasks.execute, "apply_async") as gateway_sender,
        patch.object(
            tasks, "execute_action", wraps=execution.execute_action
        ) as executor,
        patch.object(
            agent_reminders, "send_reminder", wraps=agent_reminders.send_reminder
        ) as provider,
    ):
        # When the actual worker handles the committed reference.
        result = tasks.run_agent(**references)

    # Then only that agent task is republished under the derived tenant queue.
    assert result == "deferred"
    fairness.assert_called_once_with(
        organization_id=graph.organization_a, queue="ai-interactive"
    )
    agent_sender.assert_called_once_with(
        kwargs=references,
        queue="ai-interactive",
        countdown=DEFER_COUNTDOWN_SECONDS,
    )
    gateway_sender.assert_not_called()
    executor.assert_not_called()
    provider.assert_not_called()
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        assert ActionProposal.objects.get(pk=proposal.pk).state == "approved"
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()
