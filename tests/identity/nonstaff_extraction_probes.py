"""Real extraction subjects for observed permission delegation decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ai import extraction, extraction_views
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.models import ExtractionCandidateRecord, ExtractionJob
from apps.ehr.models import Encounter
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.intake.models import PatientClinicEnrollment
from apps.orders.services import IdentityEvidence, place_order
from apps.orders.values import OrdersAccessDeniedError
from django.test import RequestFactory
from django.urls import resolve, reverse

from ai.support import DEFAULT_BUDGET
from ai.test_extraction_runtime import extracted_job
from auth.stepup_test_support import verified_request
from ehr.test_autosave import as_actor
from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context
from orders.support import World

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    import pytest

    from rbac_fixtures import RbacGraph

SYMBOLS = frozenset(
    {
        "apps.ai.extraction.authorized_job",
        "apps.ai.extraction.collect_extraction",
        "apps.ai.extraction.extraction_source",
        "apps.ai.extraction.review_extraction",
        "apps.ai.extraction.start_extraction",
        "apps.ai.extraction_views._workspace",
        "apps.ai.extraction_views.extraction_view",
    }
)


def extraction_calls(
    graph: RbacGraph, job: ExtractionJob, actor: UUID
) -> dict[str, Callable[[], bool]]:
    """Keep every source, job and candidate valid before authority is varied."""
    with owner_context(graph.organization_a):
        candidate = ExtractionCandidateRecord.objects.filter(job=job).get(position=0)
    path = reverse("ai:extraction", args=(graph.clinic_a,))
    authentication = verified_request(actor)
    request = RequestFactory().post(path, {"action": "refresh", "job_id": str(job.pk)})
    request.user = authentication.user
    request.session = authentication.session
    request.resolver_match = resolve(path)

    def review() -> bool:
        extraction.review_extraction(
            clinic_id=graph.clinic_a,
            candidate_id=candidate.pk,
            decision="rejected",
        )
        candidate.refresh_from_db()
        return candidate.status == "rejected" and candidate.reviewed_by_id == actor

    return {
        "apps.ai.extraction.extraction_source": lambda: (
            extraction.extraction_source(
                clinic_id=graph.clinic_a, result_version_id=job.result_version_id
            ).sha256
            == job.sha256
        ),
        "apps.ai.extraction.start_extraction": lambda: (
            extraction.start_extraction(
                clinic_id=graph.clinic_a,
                result_version_id=job.result_version_id,
                budget=DEFAULT_BUDGET,
            ).result_version_id
            == job.result_version_id
        ),
        "apps.ai.extraction.authorized_job": lambda: (
            extraction.authorized_job(clinic_id=graph.clinic_a, job_id=job.pk).pk
            == job.pk
        ),
        "apps.ai.extraction.collect_extraction": lambda: (
            extraction.collect_extraction(clinic_id=graph.clinic_a, job_id=job.pk).pk
            == job.pk
        ),
        "apps.ai.extraction.review_extraction": review,
        "apps.ai.extraction_views._workspace": lambda: (
            extraction_views._workspace(request, graph.clinic_a).status_code == 200
        ),
        "apps.ai.extraction_views.extraction_view": lambda: (
            extraction_views.extraction_view(request, graph.clinic_a).status_code == 200
        ),
    }


def extraction_probe(symbol: str, invoke: Callable[[], bool]) -> DifferentialProbe:
    """Observe the real callable even when its boundary translates a denial."""

    def call() -> bool:
        try:
            return invoke()
        except (
            CurrentActorError,
            GatewayAccessDeniedError,
            OrdersAccessDeniedError,
            ClinicalAccessDeniedError,
        ):
            return False

    return DifferentialProbe(symbol, call, expected=False)


def extraction_probes(
    graph: RbacGraph,
    actor: UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    encounter_id: UUID,
) -> list[DifferentialProbe]:
    """Reuse the census's real encounter and one collected extraction job."""
    with owner_context(graph.organization_a):
        encounter = Encounter.objects.get(
            pk=encounter_id,
            clinic_id=graph.clinic_a,
            physician_id=graph.physician,
            appointment__isnull=False,
        )
        enrolled = PatientClinicEnrollment.objects.select_related("patient").get(
            clinic_id=graph.clinic_a, patient_id=encounter.patient_id
        )
        job = (
            ExtractionJob.objects.filter(
                clinic_id=graph.clinic_a,
                user_id=graph.physician,
                patient_enrollment_id=enrolled.pk,
                result_version__isnull=False,
            )
            .order_by("created_at", "pk")
            .first()
        )
    if job is None:
        with owner_context(graph.organization_a):
            identity = IdentityEvidence(
                birth_date=enrolled.patient.birth_date,
                name=enrolled.patient.full_name,
            )
        with as_actor(graph, graph.physician):
            order = place_order(
                clinic_id=graph.clinic_a,
                enrollment_id=enrolled.pk,
                exam_codes=("cbc",),
                command_id=uuid4(),
            )
        job = extracted_job(
            graph, monkeypatch, world=World(order, identity, enrolled.pk)
        )
    calls = extraction_calls(graph, job, actor)
    assert calls.keys() == SYMBOLS
    return [extraction_probe(symbol, calls[symbol]) for symbol in sorted(SYMBOLS)]
