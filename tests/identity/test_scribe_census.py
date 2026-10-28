"""The focused note-authority proof runs the real, shared-subject census."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.utils import timezone

from identity.nonstaff_census import run_nonstaff_census
from identity.nonstaff_draft_probes import draft_calls, draft_probe, draft_subjects
from identity.permission_support import permission_context
from identity.test_permission_parity import INVENTORY
from identity.test_service_principals import agent_password
from scheduling.clock_support import frozen_sql_clocks
from scribe.draft_support import seed_draft_world
from scribe.test_ingest import media

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("agent_password", "media")
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


def test_draft_adapters_allow_the_clinician_and_refuse_the_nonstaff_actor(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = seed_draft_world(rbac_graph, monkeypatch)
    subject = draft_subjects(
        rbac_graph, version=world.version, transcript=world.transcript
    )
    denied = User.objects.create(username="synthetic-draft-census-nonstaff")
    for actor, expected in ((rbac_graph.physician, True), (denied.pk, False)):
        calls = draft_calls(subject, actor)
        assert len(calls) == 8
        for symbol, invoke in calls.items():
            with permission_context(rbac_graph, actor):
                assert draft_probe(symbol, invoke).invoke() is expected, symbol


def test_real_nonstaff_census_observes_the_note_reader(
    agent_password: None,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    with frozen_sql_clocks(superuser_database_url, timezone.now()):
        report = run_nonstaff_census(INVENTORY["candidates"], rbac_graph, monkeypatch)
    symbol = "apps.scribe.draft_sources.note_frame"
    assert symbol + "#observed" in report.executed
    assert report.observations[symbol]
    symbols = {
        row["symbol"] + "#observed"
        for row in INVENTORY["candidates"]
        if row["symbol"].startswith(
            (
                "apps.scribe.draft_sources.",
                "apps.scribe.drafting.",
                "apps.scribe.review_views.",
                "apps.scribe.reviews.",
            )
        )
    }
    assert len(symbols) == 8
    assert symbols <= report.executed
