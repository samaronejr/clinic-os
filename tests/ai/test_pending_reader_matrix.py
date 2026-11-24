"""Pair each registered inference source with every real presentation category."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

import pytest
from apps.ai.contracts import Purpose
from apps.ai.evaluation.services import PURPOSE_CAPABILITIES

from ai import pending_admin_cases as admin
from ai import pending_clinical_cases as clinical
from ai.pending_reader_probe import ArchiveProbe, ReaderCase
from ai.test_revenue_cycle import finance_setup, world
from analytics.test_analyst import analyst_world
from interop.test_org_export import dispatched
from scribe.test_draft_runtime import draft_world
from scribe.test_ingest import media

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = (
    "analyst_world",
    "dispatched",
    "draft_world",
    "finance_setup",
    "media",
    "world",
)
pytestmark = pytest.mark.django_db(transaction=True)


def _case(
    purpose: Purpose,
    g: RbacGraph,
    patch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> ReaderCase:
    match purpose:
        case Purpose.BRIEF | Purpose.DECISION_SUPPORT:
            result = clinical.chart_case(g, patch, purpose)
        case Purpose.SCRIBE_ASR:
            result = clinical.asr_case(g, patch)
        case Purpose.SCRIBE_DRAFT:
            result = clinical.draft_case(request.getfixturevalue("draft_world"), patch)
        case Purpose.EXTRACTION:
            result = clinical.extraction_case(g, patch)
        case Purpose.FRONT_DESK:
            result = admin.front_case(g, patch, request)
        case Purpose.REVENUE_CYCLE:
            result = admin.revenue_case(request.getfixturevalue("world"))
        case Purpose.CARE_COORDINATION:
            result = admin.care_case(
                g, patch, request.getfixturevalue("synthetic_secret_backend")
            )
        case Purpose.ANALYST:
            result = admin.analyst_case(request.getfixturevalue("analyst_world"))
        case unknown:
            assert_never(unknown)
    return result


@pytest.mark.parametrize("purpose", tuple(map(Purpose, PURPOSE_CAPABILITIES)), ids=str)
@pytest.mark.parametrize("reader", ["page", "partial", "export_download"])
def test_pending_output_reader_matrix(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    purpose: Purpose,
    reader: str,
) -> None:
    assert set(Purpose) == set(map(Purpose, PURPOSE_CAPABILITIES))
    if reader == "export_download":
        request.getfixturevalue("dispatched")
    case = _case(purpose, rbac_graph, monkeypatch, request)
    if reader != "export_download":
        assert case.displays(reader == "partial")
        case.change_choice()
        assert case.displays(reader == "partial") is case.remains_visible
        return
    archive = ArchiveProbe.create(rbac_graph)
    # An aggregate has no patient choice. Keep it visible in both formats even
    # when the *same archive* must withhold a different, patient-linked row.
    retained = None
    if purpose is Purpose.ANALYST:
        retained = case
        case = admin.front_case(rbac_graph, monkeypatch, request)
    archive.check(case.relation, case.keys, visible=True, alongside=retained)
    case.change_choice()
    archive.check(
        case.relation, case.keys, visible=case.remains_visible, alongside=retained
    )
