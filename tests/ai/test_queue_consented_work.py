"""Queue visibility of an approved Finance proposal that has not executed."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.ai.models import ActionExecution, ActionProposal

from ai.pending_display_support import withdraw
from ai.test_pending_render_commit import fetch_after_load, proposal_page
from ai.test_revenue_cycle import approve, finance_setup, world
from auth.stepup_test_support import verified_request
from billing.journal_support import finance_scope
from identity.lifecycle_test_support import verified_client

if TYPE_CHECKING:
    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("transport", ["native", "htmx"])
def test_withdrawn_approved_proposal_disappears_from_inflight_queue(
    world: RevenueWorld, transport: str
) -> None:
    page = proposal_page(world, "queue", transport)
    graph = world.setup.graph
    request = verified_request(graph.clinic_admin, persisted=True)
    with finance_scope(world.setup):
        proposal = ActionProposal.objects.get(pk=page.proposal)
        assert proposal.state == "validated"
        approve(world, proposal, request)
        assert ActionProposal.objects.filter(pk=proposal.pk, state="approved").exists()
        assert not ActionExecution.objects.filter(proposal_id=proposal.pk).exists()

    client = verified_client(graph)
    client.defaults["HTTP_HX_REQUEST"] = "true" if transport == "htmx" else "false"
    page = replace(page, client=client)
    marker = f'data-agent-proposal="{page.proposal}"'.encode()
    status, before = page.fetch()
    assert status == 200
    assert marker + b' data-agent-state="approved"' in before

    # The same Event fence on both trees runs the ordinary, independent
    # patient revocation immediately after the authorized row is loaded.
    status, during = fetch_after_load(
        page, lambda: withdraw(page.patient, page.receipt)
    )
    assert status == 200
    with finance_scope(world.setup):
        assert not ActionProposal.objects.filter(pk=page.proposal).exists()
    status, fresh = page.fetch()
    assert status == 200
    assert marker not in fresh
    assert marker not in during, (
        "withdrawn proposal still present on the in-flight queue"
    )
