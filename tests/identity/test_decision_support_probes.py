"""Require real allow and deny outcomes from every new differential adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.utils import timezone

from ai.decision_support_helpers import enable, world_for
from identity.nonstaff_census import run_nonstaff_census
from identity.nonstaff_decision_support_probes import decision_calls, decision_probe
from identity.permission_support import permission_context
from identity.test_permission_parity import INVENTORY
from identity.test_service_principals import agent_password
from scheduling.clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("agent_password",)
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


def test_decision_adapters_allow_current_clinician_and_refuse_membership_free_actor(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_for(rbac_graph, monkeypatch)
    enable(world)
    denied = User.objects.create(username="synthetic-decision-no-membership")
    for actor, expected in ((rbac_graph.physician, True), (denied.pk, False)):
        calls = decision_calls(world, actor)
        for symbol, invoke in calls.items():
            with permission_context(rbac_graph, actor):
                assert decision_probe(symbol, invoke).invoke() is expected, symbol


def test_decision_adapters_compose_with_primary_nonstaff_census(
    agent_password: None,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    with frozen_sql_clocks(superuser_database_url, timezone.now()):
        report = run_nonstaff_census(INVENTORY["candidates"], rbac_graph, monkeypatch)
    symbols = {
        row["symbol"] + "#observed"
        for row in INVENTORY["candidates"]
        if row["symbol"].startswith(
            (
                "apps.ai.decision_policy.",
                "apps.ai.decision_support.",
                "apps.ai.decision_views.",
            )
        )
    }
    assert len(symbols) == 5
    assert symbols <= report.executed
