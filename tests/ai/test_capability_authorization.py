"""Run each registered capability up to a denied common authority decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from apps.ai import authorization
from apps.ai.contracts import Purpose
from apps.ai.evaluation.services import PURPOSE_CAPABILITIES
from apps.ai.models import AIArtifact
from apps.analytics.analyst import request_analyst

from ai import brief_support, care_support, decision_support_helpers, revenue_support
from ai.test_extraction_runtime import extracted_job
from ai.test_front_desk import desk_world, say
from analytics.test_analyst import analyst_world
from analytics.test_metrics import metric_query
from billing.journal_support import finance_setup
from ehr.test_autosave import as_actor
from scribe import draft_support
from scribe.test_ingest import media

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("analyst_world", "finance_setup", "media")
pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True)
class Fixture:
    graph: RbacGraph
    patch: pytest.MonkeyPatch
    root: Path
    settings: SettingsWrapper
    request: pytest.FixtureRequest


def _trigger(purpose: Purpose, f: Fixture) -> Callable[[], object]:
    g = f.graph
    match purpose:
        case Purpose.BRIEF:
            brief = brief_support.seeded(g, f.patch)
            return lambda: brief_support.generate(brief)
        case Purpose.DECISION_SUPPORT:
            decision = decision_support_helpers.world_for(g, f.patch)
            decision_support_helpers.enable(decision)
            return lambda: decision_support_helpers.generate(decision)
        case Purpose.EXTRACTION:
            return lambda: extracted_job(g, f.patch)
        case Purpose.CARE_COORDINATION:
            care = care_support.care_world(g, f.patch, f.root)
            return lambda: care_support.request_and_collect(care)
        case Purpose.SCRIBE_ASR | Purpose.SCRIBE_DRAFT:
            return _scribe_trigger(purpose, f)
        case _:
            return _administrative_trigger(purpose, f)


def _scribe_trigger(purpose: Purpose, f: Fixture) -> Callable[[], object]:
    if purpose is Purpose.SCRIBE_ASR:
        # The drafting fixture starts by transcribing real consented audio.
        return lambda: draft_support.seed_draft_world(f.graph, f.patch)
    draft = draft_support.seed_draft_world(f.graph, f.patch)
    return lambda: draft_support.queue(draft)


def _administrative_trigger(
    purpose: Literal[Purpose.FRONT_DESK, Purpose.REVENUE_CYCLE, Purpose.ANALYST],
    f: Fixture,
) -> Callable[[], object]:
    g = f.graph
    match purpose:
        case Purpose.FRONT_DESK:
            desk = desk_world(g, f.settings, f.patch)
            return lambda: say(desk, "Olá")
        case Purpose.REVENUE_CYCLE:
            f.settings.INSURANCE_SYNTHETIC_ENABLED = True
            revenue = revenue_support.revenue_world(
                f.request.getfixturevalue("finance_setup"), f.patch, f.root
            )
            return lambda: revenue_support.request_and_collect(revenue)
        case Purpose.ANALYST:
            analyst = f.request.getfixturevalue("analyst_world")

            def ask() -> object:
                with as_actor(g, analyst.administrator):
                    return request_analyst(
                        clinic_id=g.clinic_a,
                        question="faltas",
                        query=metric_query("no_show"),
                    )

            return ask
        case unreachable:
            assert_never(unreachable)


class BoundaryReachedError(Exception):
    """Deliberately not a domain exception: a fallback cannot swallow this probe."""


@pytest.mark.parametrize("purpose", [Purpose(key) for key in PURPOSE_CAPABILITIES])
def test_registered_domain_entry_cannot_bypass_authorization(
    purpose: Purpose,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    assert set(PURPOSE_CAPABILITIES) == {p.value for p in Purpose}
    trigger = _trigger(
        purpose,
        Fixture(
            rbac_graph,
            monkeypatch,
            request.getfixturevalue("synthetic_secret_backend"),
            request.getfixturevalue("settings"),
            request,
        ),
    )
    original = authorization.authorize_capability

    def refuse(
        candidate: Purpose,
        subject: authorization.StaffInput | authorization.MessageSubject,
    ) -> object:
        if candidate is not purpose:
            return original(candidate, subject)
        if isinstance(subject, authorization.MessageSubject):
            assert purpose is Purpose.FRONT_DESK
            assert subject.kind == "message"
        else:
            # Inside the real runtime transaction, before even an uncommitted
            # first input can be written. Merely checking at dispatch is too late.
            assert not AIArtifact.objects.filter(purpose=purpose.value).exists()
        raise BoundaryReachedError

    monkeypatch.setattr(authorization, "authorize_capability", refuse)
    with pytest.raises(BoundaryReachedError):
        trigger()
