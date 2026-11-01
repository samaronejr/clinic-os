"""Real HTTP probes preserve state across agent command and policy refusals."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai import agent_views
from apps.ai.agent_policies import publish_agent_policy
from apps.ai.models import ActionProposal
from django.conf import settings as django_settings
from django.db import connection
from django.urls import reverse

from ai.agent_support import agent_world, propose
from core.test_navigation import _client_for, _get, _post
from core.test_workspace_boundaries import _normalized_denial, _pin_patient
from core.test_workspace_in_view_refusals import assert_session_read_only
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, runtime_role
from scribe.test_refusals import snapshot as committed_state

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from apps.ai.agent_policies import PolicyTerms
    from apps.ai.models import AgentPolicyVersion
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "hx"])
def test_agent_command_and_policy_refusals_preserve_committed_state(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    enhanced: bool,
) -> None:
    subject = agent_world(
        rbac_graph, monkeypatch, synthetic_secret_backend, autonomy="A1"
    )
    proposal = propose(subject)
    client, actor = _client_for(rbac_graph, "clinic_admin")
    queue = reverse("ai:agents", args=(rbac_graph.clinic_a,))
    settings = reverse("ai:agent-settings", args=(rbac_graph.clinic_a,))
    valid = {
        "proposal_id": str(proposal.pk),
        "digest": proposal.digest,
        "command": "reject",
    }
    policy = {
        "capability": "front_desk",
        "front_desk-expected_version": "0",
        "front_desk-autonomy_ceiling": "A0",
        "front_desk-budget_actions": "20",
        "front_desk-budget_micros": "20",
        "front_desk-timeout_seconds": "30",
        "front_desk-compensation": "manual",
    }

    def send(url: str, payload: dict[str, str]) -> _MonkeyPatchedWSGIResponse:
        form = {
            "csrfmiddlewaretoken": client.cookies[
                django_settings.CSRF_COOKIE_NAME
            ].value,
            **payload,
        }
        with runtime_role(), fixed_otp_time():
            return client.post(
                url, form, HTTP_HX_REQUEST="true" if enhanced else "false"
            )

    assert _get(client, queue).status_code == 200
    _pin_patient(client, rbac_graph, actor, "clinic_admin")
    snapshot = dict(client.session)
    before = committed_state()
    refused = []
    for identifier in (uuid4(), uuid4()):
        response = send(queue, {**valid, "proposal_id": str(identifier)})
        assert response.status_code == 403
        assert_session_read_only(client, response, snapshot)
        assert committed_state() == before
        refused.append(_normalized_denial(response))
    assert refused[0] == refused[1]
    for url, payload, expected in (
        (queue, {**valid, "digest": "f" * 64}, 409),
        (settings, {**policy, "extra": "synthetic"}, 400),
        (settings, {"capability": "front_desk"}, 400),
        (settings, policy, 409),
    ):
        response = send(url, payload)
        assert response.status_code == expected
        assert_session_read_only(client, response, snapshot)
        assert committed_state() == before
    with owner_context(rbac_graph.organization_a):
        assert ActionProposal.objects.get(pk=proposal.pk).state == proposal.state


def test_agent_policy_refuses_authority_revoked_after_admission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    client, actor = _client_for(rbac_graph, "clinic_admin")
    url = reverse("ai:agent-settings", args=(rbac_graph.clinic_a,))
    assert _get(client, url).status_code == 200
    snapshot = dict(client.session)
    publish = publish_agent_policy
    entered: list[UUID] = []
    admitted: list[dict[str, str]] = []

    def revoke_and_publish(
        *, clinic_id: UUID, expected_version: int, terms: PolicyTerms
    ) -> AgentPolicyVersion:
        with psycopg.connect(
            superuser_database_url,
            dbname=str(connection.settings_dict["NAME"]),
            autocommit=True,
        ) as owner:
            changed = owner.execute(
                "UPDATE clinic_app.identity_userclinicrole "
                "SET revoked_at=statement_timestamp() "
                "WHERE user_id=%s AND clinic_id=%s AND revoked_at IS NULL",
                [actor.pk, clinic_id],
            )
            assert changed.rowcount == 1
        entered.append(clinic_id)
        admitted.append(committed_state())
        return publish(
            clinic_id=clinic_id, expected_version=expected_version, terms=terms
        )

    monkeypatch.setattr(agent_views, "publish_agent_policy", revoke_and_publish)
    response = _post(
        client,
        url,
        {
            "capability": "front_desk",
            "front_desk-expected_version": "0",
            "front_desk-autonomy_ceiling": "A0",
            "front_desk-budget_actions": "20",
            "front_desk-budget_micros": "20",
            "front_desk-timeout_seconds": "30",
            "front_desk-compensation": "manual",
        },
    )
    assert entered == [rbac_graph.clinic_a]
    assert len(admitted) == 1
    assert response.status_code == 403
    assert_session_read_only(client, response, snapshot)
    assert committed_state() == admitted[0]
