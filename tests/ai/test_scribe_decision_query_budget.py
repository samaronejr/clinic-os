"""Count a real Scribe decision, excluding setup and persisted-state verification."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.models import ClinicalDocumentVersion
from apps.scribe.draft_contracts import ReviewInput
from apps.scribe.models import DraftSectionSuggestion, ReviewDecision
from apps.scribe.reviews import review_suggestion
from apps.tenancy.db import tenant_context
from django.db import connection

from ai.test_pending_query_budget import Count
from patient_service_support import runtime_role
from scribe.draft_support import generate
from scribe.test_draft_runtime import draft_world
from scribe.test_ingest import media

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.scribe.draft_contracts import Decision

    from scribe.draft_support import DraftWorld

__all__ = ("draft_world", "media")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("action", ["accepted", "modified", "rejected"])
def test_scribe_review_decision_query_count(
    draft_world: DraftWorld,
    action: Decision,
    record_property: Callable[[str, int], None],
) -> None:
    scope, graph = draft_world, draft_world.graph
    draft = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        suggestion = DraftSectionSuggestion.objects.get(
            draft=draft, section="subjective"
        )
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        replacement = "Sintético: revisão explícita." if action == "modified" else None
        expected = (replacement or suggestion.text) if action != "rejected" else None
        review = ReviewInput(
            suggestion.pk, version.revision, uuid4(), action, version.soap, replacement
        )
    count = Count()
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        connection.execute_wrapper(count),
    ):
        decision = review_suggestion(
            clinic_id=graph.clinic_a, version_id=scope.version, review=review
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        stored = ReviewDecision.objects.get(pk=decision.pk)
        assert (stored.action, stored.suggestion_id, stored.applied_text) == (
            action,
            review.suggestion_id,
            expected,
        )
        version.refresh_from_db()
        assert version.revision == review.expected_revision + int(action != "rejected")
        if expected is not None:
            assert version.soap["subjective"] == expected
    assert count.fences == 1
    record_property("queries", count.queries)
    record_property("shared_fences", count.fences)
    record_property("final_batches", count.batches)
