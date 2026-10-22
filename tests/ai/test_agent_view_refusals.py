"""Real-client refusals for every derived 4xx exit of the agent views.

Todo 39 proved these branches by calling the views directly, so the collection
refusal trace, which credits an exit only on a real client request, never saw
them. Each case here runs through the actual URL, middleware, OTP session and
tenant transaction, compares the denial across distinct causes, and proves the
refusal changed neither the session nor the agent, policy or audit rows.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.action_payload import parse_payload
from apps.ai.models import ActionProposal, AgentPolicyVersion, Approval
from apps.ai.proposals import AgentContext, propose_action
from apps.audit.models import AuditEvent
from apps.identity.models import ServicePrincipal, UserClinicRole
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from ai.agent_support import AgentWorld, propose
from ai.test_agent_execution import world
from core.test_navigation import _client_for, _csrf, _get, _reauthenticate
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, runtime_role

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

__all__ = ("world",)
pytestmark = pytest.mark.django_db(transaction=True)

# Per-render values: masked CSRF tokens and CSP nonces.
VOLATILE = re.compile(rb'(csrfmiddlewaretoken" value="|nonce=")[^"]*"')
FEEDBACK = re.compile(rb'<div class="feedback feedback--error".*?</div>', re.DOTALL)


def _post(
    client: Client, url: str, data: dict[str, object]
) -> _MonkeyPatchedWSGIResponse:
    with runtime_role(), fixed_otp_time():
        return client.post(url, data, headers=_csrf(client))


def _effects(graph: RbacGraph) -> tuple[object, ...]:
    with owner_context(graph.organization_a):
        return (
            sorted(ActionProposal.objects.values_list("id", "state")),
            Approval.objects.count(),
            sorted(AgentPolicyVersion.objects.values_list("id", "version")),
            AuditEvent.objects.count(),
        )


def _refusal(
    client: Client,
    graph: RbacGraph,
    url: str,
    data: dict[str, object],
    status: int,
) -> bytes:
    """POST once; return the comparable body of a side-effect-free refusal."""
    session = dict(client.session)
    before = _effects(graph)
    response = _post(client, url, data)
    assert response.status_code == status
    assert response.wsgi_request.session.modified is False
    assert dict(client.session) == session
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert _effects(graph) == before
    return VOLATILE.sub(rb'\1"', response.content)


def _decision(proposal_id: object, digest: str) -> dict[str, object]:
    return {"proposal_id": str(proposal_id), "digest": digest, "command": "reject"}


def _policy_form(
    *,
    expected_version: int,
    autonomy: str,
    tools: tuple[str, ...] = (),
    principal: object = "",
) -> dict[str, object]:
    return {
        "capability": "front_desk",
        "front_desk-expected_version": str(expected_version),
        "front_desk-enabled": "on",
        "front_desk-autonomy_ceiling": autonomy,
        "front_desk-tools": list(tools),
        "front_desk-principal_id": str(principal),
        "front_desk-budget_actions": "20",
        "front_desk-budget_micros": "20",
        "front_desk-timeout_seconds": "30",
        "front_desk-compensation": "manual",
    }


def _second_proposal(world: AgentWorld) -> ActionProposal:
    data = world.payload.data()
    data.update(action_id=str(uuid4()), idempotency_key=str(uuid4()))
    graph = world.gateway.graph
    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        return propose_action(
            clinic_id=graph.clinic_a,
            payload=parse_payload(json.dumps(data)),
            context=AgentContext(),
        )


def test_queue_decision_denies_unknown_and_foreign_proposals_alike(
    world: AgentWorld,
) -> None:
    # Given clinic A's pending proposal and a staff member of clinics A and B.
    graph = world.gateway.graph
    proposal = propose(world)
    client, user = _client_for(graph, "clinic_admin")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=user.pk,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    _reauthenticate(client, user.pk, "clinic_admin")
    queue = reverse("ai:agents", args=(graph.clinic_b,))
    assert _get(client, queue).status_code == 200
    # When clinic B's queue is asked to decide A's proposal or an unknown one.
    denials = {
        _refusal(client, graph, queue, _decision(selector, proposal.digest), 403)
        for selector in (proposal.pk, uuid4())
    }
    # Then both get one identical in-view denial and the proposal is untouched.
    assert len(denials) == 1
    with owner_context(graph.organization_a):
        assert ActionProposal.objects.get(pk=proposal.pk).state == proposal.state


def test_queue_digest_and_state_conflicts_share_one_conflict(
    world: AgentWorld,
) -> None:
    # Given one pending proposal and one the same clinic has already rejected.
    graph = world.gateway.graph
    pending = propose(world)
    decided = _second_proposal(world)
    client, _user = _client_for(graph, "clinic_admin")
    queue = reverse("ai:agents", args=(graph.clinic_a,))
    rejected = _post(client, queue, _decision(decided.pk, decided.digest))
    assert rejected.status_code == 302
    # When a stale digest and a repeated decision are submitted.
    conflicts = {
        _refusal(client, graph, queue, data, 409)
        for data in (
            _decision(pending.pk, "f" * 64),
            _decision(decided.pk, decided.digest),
        )
    }
    # Then both re-render the same conflict without revealing which check failed.
    assert len(conflicts) == 1
    assert FEEDBACK.search(conflicts.pop()) is not None


def test_policy_form_tampering_is_one_bad_request(rbac_graph: RbacGraph) -> None:
    # Given an otherwise acceptable default-off policy publication.
    client, _user = _client_for(rbac_graph, "clinic_admin")
    url = reverse("ai:agent-settings", args=(rbac_graph.clinic_a,))
    form = _policy_form(expected_version=0, autonomy="A0")
    # When an unknown field, a repeated field or an upload is added.
    tampered = (
        {**form, "front_desk-unexpected": "synthetic"},
        {**form, "front_desk-budget_actions": ["20", "21"]},
        {
            **form,
            "front_desk-principal_id": SimpleUploadedFile(
                "synthetic.txt", b"synthetic"
            ),
        },
    )
    denials = {_refusal(client, rbac_graph, url, data, 400) for data in tampered}
    # Then each is the same bad request, and only the untampered form publishes.
    assert len(denials) == 1
    assert _post(client, url, form).status_code == 302
    with owner_context(rbac_graph.organization_a):
        published = AgentPolicyVersion.objects.filter(clinic_id=rbac_graph.clinic_a)
        assert published.count() == 1


def test_policy_principal_outside_the_clinic_is_one_denial(
    world: AgentWorld,
) -> None:
    # Given an active machine principal that belongs to clinic B, not clinic A.
    graph = world.gateway.graph
    with owner_context(graph.organization_a):
        foreign = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            name="synthetic-agent-b",
            db_identity=f"clinic_agent_t40_{uuid4().hex[:12]}",
            purpose="agent-execution",
            grant_set_version=2,
        )
    client, _user = _client_for(graph, "clinic_admin")
    url = reverse("ai:agent-settings", args=(graph.clinic_a,))
    tools = ("comms.synthetic_reminder",)
    # When a valid policy names B's principal or an unknown one for clinic A.
    denials = {
        _refusal(
            client,
            graph,
            url,
            _policy_form(
                expected_version=world.policy.version,
                autonomy="A3",
                tools=tools,
                principal=principal,
            ),
            403,
        )
        for principal in (foreign.pk, uuid4())
    }
    # Then both are one denial; A's own principal is accepted.
    assert len(denials) == 1
    accepted = _post(
        client,
        url,
        _policy_form(
            expected_version=world.policy.version,
            autonomy="A3",
            tools=tools,
            principal=world.principal.pk,
        ),
    )
    assert accepted.status_code == 302


def test_policy_form_errors_and_conflicts_rerender_without_effects(
    world: AgentWorld,
) -> None:
    # Given clinic A's published front desk policy.
    graph = world.gateway.graph
    client, _user = _client_for(graph, "clinic_admin")
    url = reverse("ai:agent-settings", args=(graph.clinic_a,))
    current = world.policy.version
    # When the form is invalid, stale, or names a tool A0 cannot hold.
    invalid = _refusal(
        client,
        graph,
        url,
        _policy_form(expected_version=current, autonomy="A9"),
        400,
    )
    conflicts = {
        found.group(0)
        for data in (
            _policy_form(expected_version=current - 1, autonomy="A0"),
            _policy_form(
                expected_version=current,
                autonomy="A0",
                tools=("workflows.review_task",),
            ),
        )
        for found in (FEEDBACK.search(_refusal(client, graph, url, data, 409)),)
        if found is not None
    }
    # Then each re-renders its form, and both conflicts give one feedback block.
    assert FEEDBACK.search(invalid) is not None
    assert len(conflicts) == 1
