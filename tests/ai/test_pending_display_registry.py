"""Read already-generated material after loss of authority, for each real purpose."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

import pytest
from apps.ai.contracts import Purpose
from apps.ai.evaluation.services import PURPOSE_CAPABILITIES

from ai import pending_display_support as probes
from ai import source_visit_reads
from ai.test_chart_consent import (
    test_revocation_fences_queued_work_and_retained_output as chart_read,
)
from ai.test_revenue_cycle import world
from analytics.test_analyst import analyst_world
from analytics.test_analyst import (
    test_gateway_runs_offline_and_only_human_acceptance_executes_metrics as probe,
)
from billing.journal_support import finance_setup
from scribe.test_draft_runtime import draft_world
from scribe.test_ingest import media
from scribe.test_pending_display import (
    test_withdrawal_hides_pending_drafts_but_preserves_clinician_text as scribe_read,
)
from scribe.test_pending_transcript import partial_transcript_read

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("analyst_world", "draft_world", "finance_setup", "media", "world")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("purpose", "choice"),
    [
        *(pytest.param(Purpose(p), "retained", id=p) for p in PURPOSE_CAPABILITIES),
        # A one-case-per-purpose map proved withdrawal but missed the separate
        # source encounter. Derive that dimension from admitted A1 tool terms.
        *(
            pytest.param(p, reader, id=f"{p.value}-visit-{reader}")
            for p in source_visit_reads.visit_proposal_purposes()
            for reader in (
                ("clinician", "queue") if p is Purpose.CARE_COORDINATION else ("queue",)
            )
        ),
    ],
)
def test_governed_pending_material_is_rechecked_at_read_time(
    purpose: Purpose,
    choice: str,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    assert set(Purpose) == set(map(Purpose, PURPOSE_CAPABILITIES))
    if choice != "retained":
        source_visit_reads.check_source_visit(
            purpose, rbac_graph, monkeypatch, request, choice
        )
        return
    match purpose:
        case Purpose.SCRIBE_ASR:
            partial_transcript_read(rbac_graph, monkeypatch)
        case Purpose.SCRIBE_DRAFT:
            scribe_read(
                request.getfixturevalue("draft_world"),
                monkeypatch,
                "native",
                "withdraw",
            )
        case Purpose.BRIEF | Purpose.DECISION_SUPPORT:
            chart_read(rbac_graph, monkeypatch, purpose, "retained")
        case Purpose.FRONT_DESK:
            probes.front_desk(rbac_graph, monkeypatch, request)
        case Purpose.REVENUE_CYCLE:
            probes.finance(rbac_graph, monkeypatch, request)
        case Purpose.CARE_COORDINATION:
            probes.care(rbac_graph, monkeypatch, request)
        case Purpose.EXTRACTION:
            probes.extraction(rbac_graph, monkeypatch)
        case Purpose.ANALYST:
            # No patient subject: this aggregate descriptor is requester-bound.
            # Test its real read boundary rather than fabricating a patient.
            probe(request.getfixturevalue("analyst_world"))
        case unhandled:
            assert_never(unhandled)
