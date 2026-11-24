"""The actual pending SQL decisions must also reject an inactive original actor."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.models import AIInvocation
from apps.consent.services import record_ai_disclosure
from apps.tenancy.envelope import _kek

from ai import brief_support
from ai.chart_consent_support import accept_ai
from ai.pending_display_support import withdraw
from ai.revenue_support import request_and_collect
from ai.test_revenue_cycle import world
from billing.journal_support import finance_scope, finance_setup
from ehr.test_autosave import as_actor
from identity.test_sql_inactive_actor import _decide, _set_active

if TYPE_CHECKING:
    from uuid import UUID

    from ai.revenue_support import RevenueWorld
    from rbac_fixtures import RbacGraph

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


def test_finance_guide_and_pending_output_refuse_the_inactive_sql_actor(
    world: RevenueWorld,
) -> None:
    setup, guide = world.setup, world.cycle.guide
    g = setup.graph
    accept_ai(g, setup.appointment.enrollment_id)
    review = request_and_collect(world)
    assert review.state == "ready"
    assert review.output_id is not None
    assert review.invocation_id is not None
    with finance_scope(setup):
        source = AIInvocation.objects.get(pk=review.invocation_id).inputs_ref_id
    params: dict[str, UUID | str] = {
        "clinic": g.clinic_a,
        "guide": guide.pk,
        "visit": guide.encounter_id,
        "source": source,
        "output": review.output_id,
        "kek": _kek(),
    }
    actor = g.clinic_admin
    try:
        for active in (True, False, True):
            _set_active(actor, active=active)
            assert (
                _decide(
                    g,
                    actor,
                    "SELECT clinic_app.ai_guide_consent("
                    "%(clinic)s,%(guide)s,%(visit)s,%(source)s,%(kek)s)",
                    params,
                )
                is active
            )
            assert (
                _decide(
                    g,
                    actor,
                    "SELECT clinic_app.ai_pending_visible('ai_aiartifact',%(output)s)",
                    params,
                )
                is active
            )
    finally:
        _set_active(actor, active=True)


def test_content_free_withdrawal_notice_refuses_the_inactive_sql_actor(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g = rbac_graph
    clinical = brief_support.without_patient_choice(g, monkeypatch)
    patient, receipt = accept_ai(g, clinical.chart.enrollment)
    visit = clinical.chart.version.document.encounter_id
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a, encounter_id=visit, informed=True, refused=False
        )
    queued = brief_support.generate(clinical)
    assert queued.invocation_id is not None
    with as_actor(g, g.physician):
        source = AIInvocation.objects.get(pk=queued.invocation_id).inputs_ref_id
    withdraw(patient, receipt)
    # Before flipping activity, the original requester is genuinely entitled
    # to this one-bit refusal for this same withheld, session-bound input.
    params: dict[str, UUID | str] = {
        "clinic": g.clinic_a,
        "source": source,
        "visit": visit,
        "enrolled": clinical.chart.enrollment,
        "binding": brief_support.BINDING,
        "kek": _kek(),
    }
    try:
        for active in (True, False, True):
            _set_active(g.physician, active=active)
            assert (
                _decide(
                    g,
                    g.physician,
                    "SELECT clinic_app.ai_pending_refusal("
                    "%(clinic)s,%(source)s,'brief',%(enrolled)s,%(visit)s,NULL,"
                    "%(binding)s,%(kek)s)",
                    params,
                )
                is active
            )
    finally:
        _set_active(g.physician, active=True)
