"""The front-desk browser fixture provisions the authority its policy needs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.evaluation_models import ModelChangeRequest
from apps.ai.models import ModelConfiguration
from apps.ai.owner import set_global_switch, transition_model
from apps.identity.models import Clinic, User, UserClinicRole

from ai.test_front_desk import agent_replies, desk_world, say
from identity.permission_support import owner_context
from renewal.front_desk_fixture import seed

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _browser_administrator(graph: RbacGraph) -> User:
    """Administer clinics A and B, as seed_ai_admin does for the browser."""
    actor = User.objects.create(username=f"synthetic-desk-admin-{uuid4().hex}")
    with owner_context(graph.organization_a):
        for clinic in (graph.clinic_a, graph.clinic_b):
            for role in ("clinic_admin", "org_admin"):
                UserClinicRole.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=clinic,
                    user=actor,
                    role=role,
                )
    return actor


def test_seed_publishes_the_policy_after_another_scenario_added_a_clinic(
    rbac_graph: RbacGraph,
) -> None:
    actor = _browser_administrator(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        # An earlier scenario adds a unit the administrator holds no role in.
        added = Clinic.objects.create(
            organization_id=rbac_graph.organization_a,
            name="Synthetic added unit",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
    receipt = seed(actor.pk, rbac_graph.clinic_a, rbac_graph.organization_a)
    assert receipt["synthetic"] is True
    with owner_context(rbac_graph.organization_a):
        assert UserClinicRole.objects.filter(
            user=actor, clinic=added, role="org_admin"
        ).exists()


def test_seeded_fixture_publishes_an_assistant_reply_and_stays_manual_when_off(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A linked WhatsApp patient with the agent policy unpublished, and, as in
    # the browser database, no active front-desk model.
    desk = desk_world(rbac_graph, settings, monkeypatch, enabled=False)
    for active in ModelConfiguration.objects.filter(
        purpose="front_desk", state="active"
    ):
        transition_model(configuration_id=active.pk, state="retired")
    actor = _browser_administrator(rbac_graph)
    seed(actor.pk, rbac_graph.clinic_a, rbac_graph.organization_a)
    reply = say(desk, "Qual o horário de funcionamento?")
    assert reply is not None
    assert "8h" in reply
    # The reply was admitted by a recorded promotion of the active model.
    rollout = ModelConfiguration.objects.get(purpose="front_desk", state="active")
    with owner_context(rbac_graph.organization_a):
        stages = set(
            ModelChangeRequest.objects.filter(
                configuration=rollout, approved_by__isnull=False
            ).values_list("stage", flat=True)
        )
    assert stages == {"shadow", "reviewed_live", "monitored_autonomy"}
    # AI off: the same admitted desk publishes nothing further.
    published = len(agent_replies(desk.graph))
    set_global_switch(enabled=False, approved_regions=("br-synthetic",))
    assert say(desk, "Qual o horário de funcionamento?") is None
    assert len(agent_replies(desk.graph)) == published
