"""Raw runtime reads cannot expose any retained agent record across tenants."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.action_recovery import compensate_action
from apps.comms import agent_reminders
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection

from ai.agent_support import approve, propose
from ai.test_agent_execution import agent_password, execute, machine_login, world
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from ai.agent_support import AgentWorld

__all__ = ("agent_password", "machine_login", "world")
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


@pytest.fixture
def retained_agent_records(
    world: AgentWorld, monkeypatch: pytest.MonkeyPatch
) -> AgentWorld:
    proposal = propose(world)
    approve(world, proposal)
    original = agent_reminders.send_reminder

    def failed(*, provider_key: UUID, template: str) -> agent_reminders.ReminderOutcome:
        original(provider_key=provider_key, template=template)
        return agent_reminders._outcome(provider_key, "failed")

    monkeypatch.setattr(agent_reminders, "send_reminder", failed)
    assert execute(world, proposal.pk).state == "failure"
    assert (
        compensate_action(
            clinic_id=world.gateway.graph.clinic_a,
            proposal_id=proposal.pk,
            principal_id=world.principal.pk,
        ).state
        == "compensated"
    )
    graph = world.gateway.graph
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            user_id=world.gateway.administrator,
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            role="owner",
            workflow_bundle_version=2,
        )
    return world


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("SELECT id FROM clinic_app.ai_agentpolicyversion", id="policy"),
        pytest.param("SELECT id FROM clinic_app.ai_agentrun", id="run"),
        pytest.param("SELECT id FROM clinic_app.ai_actionproposal", id="proposal"),
        pytest.param("SELECT id FROM clinic_app.ai_approval", id="approval"),
        pytest.param("SELECT id FROM clinic_app.ai_actionexecution", id="execution"),
        pytest.param("SELECT id FROM clinic_app.ai_actionreceipt", id="receipt"),
        pytest.param("SELECT id FROM clinic_app.ai_compensation", id="compensation"),
    ],
)
def test_each_agent_table_hides_foreign_rows_from_the_same_authorized_actor(
    retained_agent_records: AgentWorld, statement: str
) -> None:
    # Given populated records and one actor authorized in both organizations.
    subject = retained_agent_records
    graph = subject.gateway.graph
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
        cursor.execute(statement)
        assert cursor.fetchall()

    # When the same raw query runs in the other authorized tenant context.
    with (
        runtime_role(),
        tenant_context(subject.gateway.administrator, graph.organization_b),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
        cursor.execute(statement)

        # Then RLS, not an ORM predicate or an empty fixture, hides every row.
        assert cursor.fetchall() == []
