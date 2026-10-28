"""Real-role confirmation gates; execution belongs to the root DB lane."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.contracts import GatewayAccessDeniedError, Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.extraction import collect_extraction, review_extraction, start_extraction
from apps.ai.extraction_views import extraction_view
from apps.ai.models import ExtractionCandidateRecord, ExtractionJob
from apps.ai.policy import OrganizationPolicy
from apps.core.integration import execute_operation
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import User
from apps.orders.models import ResultAcknowledgment, ResultObservation, ResultQuarantine
from apps.orders.services import IdentityEvidence, ingest_result
from apps.orders.values import OrdersAccessDeniedError
from django.db import DatabaseError, connection, transaction
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone

from ai.support import DEFAULT_BUDGET, model, seed
from ai.test_extraction import pdf_fixture
from auth.stepup_test_support import verified_request
from ehr.test_autosave import as_actor
from orders.support import ordered, submission
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from orders.support import World
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def extracted_job(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: bytes | None = None,
    world: World | None = None,
) -> ExtractionJob:
    gateway = seed(rbac_graph, monkeypatch)
    model(purpose=Purpose.EXTRACTION)
    with as_actor(rbac_graph, gateway.administrator):
        configuration = latest_configuration(rbac_graph.clinic_a)
        assert configuration is not None
        set_organization_policy(
            clinic_id=rbac_graph.clinic_a,
            expected_version=configuration.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=(Purpose.EXTRACTION.value,),
                monthly_budget_micros=10_000_000,
                purpose_budgets=((Purpose.EXTRACTION.value, 10_000_000),),
            ),
        )
    world = ordered(rbac_graph) if world is None else world
    with as_actor(rbac_graph, rbac_graph.physician):
        result = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(
                submission(world),
                data=pdf_fixture(rotation=90) if data is None else data,
            ),
        )
        job = start_extraction(
            clinic_id=rbac_graph.clinic_a,
            result_version_id=result.version_id,
            budget=DEFAULT_BUDGET,
        )
        operation = job.invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation_id=operation) == "succeeded"
    with as_actor(rbac_graph, rbac_graph.physician):
        collect_extraction(clinic_id=rbac_graph.clinic_a, job_id=job.pk)
    return job


def test_five_confirmations_import_exact_values_without_acknowledgment(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    assert job.result_version_id is not None
    with as_actor(rbac_graph, rbac_graph.physician):
        rows = list(
            ExtractionCandidateRecord.objects.filter(job=job).order_by("position")
        )
        assert len(rows) == 5
        assert not ResultObservation.objects.filter(
            version_id=job.result_version_id
        ).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            ExtractionCandidateRecord.objects.filter(pk=rows[0].pk).update(
                status="confirmed",
                observation_id=uuid4(),
                reviewed_by_id=rbac_graph.physician,
                reviewed_at=timezone.now(),
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ResultObservation.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                version_id=job.result_version_id,
                position=0,
                source={"code": "glucose", "value": "999", "unit": "mg/dL"},
            )
        with pytest.raises(GatewayAccessDeniedError):
            review_extraction(
                clinic_id=rbac_graph.clinic_b,
                candidate_id=rows[0].pk,
                decision="confirmed",
            )
        for row in rows:
            identifier = review_extraction(
                clinic_id=rbac_graph.clinic_a, candidate_id=row.pk, decision="confirmed"
            )
            assert identifier is not None
            assert (
                review_extraction(
                    clinic_id=rbac_graph.clinic_a,
                    candidate_id=row.pk,
                    decision="confirmed",
                )
                == identifier
            )
        observations = list(
            ResultObservation.objects.filter(version_id=job.result_version_id).order_by(
                "position"
            )
        )
        assert len(observations) == 5
        assert observations[0].source["value"] == "123,40"
        assert observations[0].source["parsed_value"] == "123.40"
        assert all(row.source["provenance"] == "imported" for row in observations)
        assert all(
            row.source["confirmer"] == str(rbac_graph.physician) for row in observations
        )
        assert not ResultAcknowledgment.objects.filter(
            version_id=job.result_version_id
        ).exists()


def test_extraction_sql_rechecks_inactive_and_foreign_actor(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        rows = list(job.candidates.order_by("position"))
        observation = review_extraction(
            clinic_id=job.clinic_id,
            candidate_id=rows[0].pk,
            decision="confirmed",
        )
    assert observation is not None
    query = (
        "SELECT clinic_app.ai_extraction_source_allowed(%s,%s,%s,NULL,%s),"
        "clinic_app.ai_extraction_job_allowed(%s),"
        "clinic_app.ai_imported_observation(%s,%s,%s)"
    )
    arguments = [
        job.clinic_id,
        job.patient_enrollment_id,
        job.result_version_id,
        job.sha256,
        job.pk,
        observation,
        job.result_version_id,
        job.clinic_id,
    ]
    for active, tenant in (
        (True, rbac_graph.organization_a),
        (True, rbac_graph.organization_b),
        (False, rbac_graph.organization_a),
    ):
        User.objects.filter(pk=rbac_graph.physician).update(is_active=active)
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true),"
                "set_config('app.current_tenant',%s,true)",
                [str(rbac_graph.physician), str(tenant)],
            )
            cursor.execute(query, arguments)
            allowed = active and tenant == rbac_graph.organization_a
            assert cursor.fetchone() == (allowed, allowed, allowed)
            if allowed:
                continue
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "SELECT * FROM clinic_app.ai_review_extraction("
                    "%s,%s,'rejected',%s,%s)",
                    [rows[1].pk, job.result_version_id, b"", ""],
                )
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "INSERT INTO clinic_app.ai_extractioncandidate"
                    "(id,organization_id,clinic_id,job_id,position,payload,status,"
                    "expires_at,created_at) VALUES(%s,%s,%s,%s,0,%s,'candidate',%s,%s)",
                    [
                        uuid4(),
                        job.organization_id,
                        job.clinic_id,
                        job.pk,
                        b"invalid",
                        rows[1].expires_at,
                        timezone.now(),
                    ],
                )


def test_native_extraction_reviews_exact_regions_and_refuses_invalid_commands(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        candidate = job.candidates.order_by("position").first()
    assert candidate is not None
    path = reverse("ai:extraction", args=(job.clinic_id,))
    cases: list[tuple[dict[str, str] | None, int]] = [
        (None, 403),
        ({"action": "refresh", "job_id": str(job.pk)}, 200),
        (
            {"action": "refresh", "job_id": str(job.pk), "page": "1", "zoom": "400"},
            200,
        ),
        (
            {
                "action": "rejected",
                "job_id": str(job.pk),
                "candidate_id": str(candidate.pk),
            },
            200,
        ),
        ({"action": "refresh", "job_id": str(job.pk), "unexpected": "data"}, 400),
        ({"action": "unknown", "job_id": str(job.pk)}, 400),
        ({"action": "refresh", "job_id": str(uuid4())}, 403),
        ({"action": "refresh", "job_id": "malformed"}, 409),
        ({"action": "refresh", "job_id": str(job.pk), "zoom": "50"}, 409),
        (
            {
                "action": "confirmed",
                "job_id": str(job.pk),
                "candidate_id": str(uuid4()),
            },
            403,
        ),
    ]
    for data, expected in cases:
        authentication = verified_request(rbac_graph.physician)
        request = (
            RequestFactory().get(path)
            if data is None
            else RequestFactory().post(path, data)
        )
        request.user = authentication.user
        request.session = authentication.session
        request.resolver_match = resolve(path)
        with as_actor(rbac_graph, rbac_graph.physician):
            response = extraction_view(request, job.clinic_id)
        assert response.status_code == expected, (
            request.method,
            data.get("action") if data is not None else None,
        )
        if data is not None and expected == 200:
            assert isinstance(response, HttpResponse)
            assert response["Cache-Control"] == "private, no-store"
            assert b"<rect" in response.content
            assert b"data:image/png;base64," in response.content


def test_wrong_patient_stays_quarantined_and_cannot_start_extraction(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        result = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(
                submission(world),
                data=pdf_fixture(),
                identity=IdentityEvidence(
                    name="Synthetic wrong patient", birth_date=date(1980, 1, 1)
                ),
            ),
        )
        assert result.state == "quarantined"
        assert ResultQuarantine.objects.filter(version_id=result.version_id).exists()
        with pytest.raises((GatewayAccessDeniedError, OrdersAccessDeniedError)):
            start_extraction(
                clinic_id=rbac_graph.clinic_a,
                result_version_id=result.version_id,
                budget=DEFAULT_BUDGET,
            )
        assert not ResultObservation.objects.filter(
            version_id=result.version_id
        ).exists()
