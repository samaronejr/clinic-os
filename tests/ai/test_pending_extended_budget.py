"""Matched, data-free SQL counts for a fenced command and a Scribe response."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.approvals import reject_action
from apps.ai.models import ActionProposal
from apps.scribe import gateway
from apps.scribe.models import DraftSectionSuggestion
from apps.tenancy.db import tenant_context
from django.db import connection
from django.utils.html import escape

from ai.revenue_support import request_and_collect
from ai.test_pending_query_budget import Count
from ai.test_revenue_cycle import finance_setup, proposal_for, world
from billing.journal_support import finance_scope
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.draft_support import generate
from scribe.test_draft_runtime import draft_world
from scribe.test_draft_views import post
from scribe.test_ingest import media
from scribe.test_pending_display import two_source_reply
from scribe.test_transcript_views import bind

if TYPE_CHECKING:
    from collections.abc import Callable

    from ai.revenue_support import RevenueWorld
    from scribe.draft_support import DraftWorld

__all__ = ("draft_world", "finance_setup", "media", "world")
pytestmark = pytest.mark.django_db(transaction=True)


def _record(count: Count, prop: Callable[[str, int], None]) -> None:
    assert count.queries > 0
    prop("queries", count.queries)
    prop("shared_fences", count.fences)
    prop("final_batches", count.batches)


def test_pending_command_query_count(
    world: RevenueWorld, record_property: Callable[[str, int], None]
) -> None:
    review = request_and_collect(world)
    with finance_scope(world.setup):
        proposal = proposal_for(world, review)
    with finance_scope(world.setup):
        pk, digest = proposal.pk, proposal.digest
        count = Count()
        with connection.execute_wrapper(count):
            reject_action(
                clinic_id=world.setup.clinic_id, proposal_id=pk, expected_digest=digest
            )
    with finance_scope(world.setup):
        assert ActionProposal.objects.get(pk=pk).state == "rejected"
    assert count.fences == 1
    _record(count, record_property)


def test_scribe_positive_query_count(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, int], None],
) -> None:
    graph = draft_world.graph
    monkeypatch.setattr(gateway, "synthetic_reply", two_source_reply)
    draft = generate(draft_world)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        row = DraftSectionSuggestion.objects.get(draft=draft, section="subjective")
        assert row.text
        marker, text = str(row.pk).encode(), escape(row.text).encode()
    with physician_client(graph) as client:
        bind(graph, draft_world.recording, client, monkeypatch)
        count = Count()
        with connection.execute_wrapper(count):
            response = post(client, draft_world)
    assert response.status_code == 200
    assert marker in response.content
    assert text in response.content
    assert count.fences == 1
    assert count.batches >= 1
    _record(count, record_property)
