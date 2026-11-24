"""Withdrawing AI choice must not withdraw a clinician's filed observation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.extraction import collect_extraction, review_extraction
from apps.orders.queries import trends

from ai.chart_consent_support import accept_ai
from ai.pending_display_support import withdraw
from ai.test_extraction_runtime import extracted_job
from ehr.test_autosave import as_actor
from orders.support import ordered

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


@pytest.mark.django_db(transaction=True)
def test_withdrawal_keeps_confirmed_observation_in_clinician_trends(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = rbac_graph
    source = ordered(g)
    patient, receipt = accept_ai(g, source.enrollment)
    job = extracted_job(g, monkeypatch, world=source)
    with as_actor(g, g.physician):
        candidates = tuple(job.candidates.order_by("position"))
        assert len(candidates) > 1
        assert not trends(clinic_id=g.clinic_a, enrollment_id=source.enrollment)
        observation = review_extraction(
            clinic_id=g.clinic_a,
            candidate_id=candidates[0].pk,
            decision="confirmed",
            manual_unit="mg/dL",
        )
        assert observation is not None
        before = trends(clinic_id=g.clinic_a, enrollment_id=source.enrollment)
        assert [(row.value, row.unit) for row in before] == [("123,40", "mg/dL")]
    withdraw(patient, receipt)
    with as_actor(g, g.physician):
        with pytest.raises(GatewayAccessDeniedError):
            collect_extraction(clinic_id=g.clinic_a, job_id=job.pk)
        assert trends(clinic_id=g.clinic_a, enrollment_id=source.enrollment) == before
