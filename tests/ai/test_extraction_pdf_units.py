"""PDF values are drafts; only clinicians can enter their units."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from apps.ai.contracts import Purpose
from apps.ai.extraction import review_extraction
from apps.ai.extraction_contracts import (
    ExtractionInputError,
    filing_payload,
    parse_proposals,
)
from apps.ai.extraction_documents import (
    document_input,
    recorded_document_extraction,
    source_regions,
    synthetic_document_extraction,
)
from apps.ai.extraction_views import extraction_view
from apps.core.secrets import secret_store
from apps.orders.models import ResultObservation
from apps.tenancy.envelope import protect
from django.db import DatabaseError, connection, transaction
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import resolve, reverse

from ai.retained_rollout_support import authorize_native
from ai.test_extraction import (
    close_unit_runs,
    layout_transition_pdf,
    pdf_with_stream,
    scan_fixture,
)
from ai.test_extraction_runtime import extracted_job
from auth.stepup_test_support import verified_request
from ehr.test_autosave import as_actor

if TYPE_CHECKING:
    from apps.ai.extraction_contracts import ExtractionCandidate
    from apps.ai.models import ExtractionJob

    from rbac_fixtures import RbacGraph

PDF = "application/pdf"
SMALL_DRAWS = {
    "plain": "(glucose: 123,40 g/dL) Tj",
    "spaced": "(glucose: 123,40  g/dL) Tj",
    "kerned": "[(glucose: 123,40 g) 20 (/dL)] TJ",
    "split-run": "(glucose: 123,40 ) Tj (g) Tj (/d) Tj (L) Tj",
}


def _pdf_proposals(data: bytes, *, rotation: int, forged: bool) -> ExtractionCandidate:
    reply = json.loads(
        synthetic_document_extraction(document_input(data=data, content_type=PDF))
    )
    (draft,) = reply["candidates"]
    if forged:
        draft.update(
            field="glucose",
            value="123,40",
            unit="g/dL",
            raw_text="glucose: 123,40 g/dL",
        )
    (candidate,) = parse_proposals(
        json.dumps(reply),
        rotations=(rotation,),
        regions=source_regions(data=data, content_type=PDF, rotations=(rotation,)),
    )
    return candidate


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("layout", SMALL_DRAWS)
def test_small_pdf_suggests_exact_value_but_never_a_unit(
    layout: str, rotation: int
) -> None:
    data = pdf_with_stream(
        stream=f"BT /F1 12 Tf 30 200 Td {SMALL_DRAWS[layout]} ET",
        size=(300, 300),
        rotation=rotation,
    )
    candidate = _pdf_proposals(data, rotation=rotation, forged=True)
    assert (
        candidate.status,
        candidate.value,
        candidate.parsed_value,
        candidate.unit,
    ) == (
        "candidate",
        "123,40",
        "123.40",
        "",
    )
    assert candidate.raw_text == "glucose: 123,40"
    with pytest.raises(ExtractionInputError):
        filing_payload(candidate, confirmer="synthetic-clinician")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("case", ["hidden-layer", "overlap", "field", "value", "page"])
def test_pdf_witnesses_have_no_confirmable_unit(case: str, rotation: int) -> None:
    if case == "hidden-layer":
        documents = [
            pdf_with_stream(
                stream="BT /F1 12 Tf 40 740 Td (glucose: 123,40) Tj 3 Tr ( g/dL) Tj ET",
                rotation=rotation,
            )
        ]
    elif case == "overlap":
        documents = [close_unit_runs(rotation=rotation, advance=0)]
    else:
        documents = [
            layout_transition_pdf(
                rotation=rotation,
                transition=case,
                offset=offset,
                combined=case == "page",
            )
            for offset in (-1, 1)
        ]
    for data in documents:
        candidate = _pdf_proposals(data, rotation=rotation, forged=True)
        assert candidate.unit == ""
        assert "g/dL" not in candidate.raw_text
        assert 0 <= candidate.region.bbox[0] < candidate.region.bbox[2] <= 10000
        with pytest.raises(ExtractionInputError):
            filing_payload(candidate, confirmer="synthetic-clinician")


def _post_review(
    graph: RbacGraph, job: ExtractionJob, fields: dict[str, str]
) -> HttpResponse:
    path = reverse("ai:extraction", args=(job.clinic_id,))
    request = RequestFactory().post(
        path,
        {"job_id": str(job.pk), **fields},
    )
    authentication = verified_request(graph.physician)
    request.user = authentication.user
    request.session = authentication.session
    request.resolver_match = resolve(path)
    with as_actor(graph, graph.physician):
        response = extraction_view(request, job.clinic_id)
    assert isinstance(response, HttpResponse)
    return response


@pytest.mark.django_db(transaction=True)
def test_pdf_swap_is_not_stored_or_rendered_as_an_ai_unit(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forged_reply(content: str) -> str:
        reply = json.loads(synthetic_document_extraction(content))
        row = reply["candidates"][0]
        row["unit"] = "g/dL"
        row["raw_text"] = "glucose: 123,40 g/dL [70,0 - 99,0]"
        return json.dumps(reply)

    monkeypatch.setattr("apps.ai.adapters.synthetic_document_extraction", forged_reply)
    job = extracted_job(rbac_graph, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.get(position=0)
        assert (row.status, row.payload["value"], row.payload["unit"]) == (
            "candidate",
            "123,40",
            "",
        )
        assert row.payload["raw_text"] == "glucose: 123,40 [70,0 - 99,0]"
        assert not ResultObservation.objects.filter(
            version_id=job.result_version_id
        ).exists()
    workspace = _post_review(rbac_graph, job, {"action": "refresh"})
    assert workspace.status_code == 200
    assert b'name="manual_unit"' in workspace.content
    assert b"g/dL" not in workspace.content
    response = _post_review(
        rbac_graph,
        job,
        {"action": "confirmed", "candidate_id": str(row.pk), "manual_unit": "mg/dL"},
    )
    assert response.status_code == 200
    with as_actor(rbac_graph, rbac_graph.physician):
        observation = ResultObservation.objects.get(version_id=job.result_version_id)
        assert (observation.source["value"], observation.source["unit"]) == (
            "123,40",
            "mg/dL",
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("terms", "status"),
    [
        ({}, 409),
        ({"unit": "g/dL"}, 400),
        ({"manual_unit": "  "}, 409),
        ({"unit": "g/dL", "manual_unit": "mg/dL"}, 400),
    ],
    ids=["omitted", "ai-unit", "blank", "mixed"],
)
def test_pdf_confirmation_refuses_requests_without_only_a_manual_unit(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    terms: dict[str, str],
    status: int,
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.get(position=0)
    assert (
        _post_review(
            rbac_graph,
            job,
            {"action": "confirmed", "candidate_id": str(row.pk), **terms},
        ).status_code
        == status
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        row.refresh_from_db()
        assert row.status == "candidate"
        assert not ResultObservation.objects.filter(
            version_id=job.result_version_id
        ).exists()


@pytest.mark.django_db(transaction=True)
def test_native_pdf_filing_rejects_a_forged_observation_unit_without_manual_entry(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.get(position=0)
        candidate = row.payload
        source = protect(
            purpose="orders.observation.source",
            plaintext=json.dumps(
                {
                    "code": candidate["field"],
                    "value": candidate["value"],
                    "parsed_value": candidate["parsed_value"],
                    "unit": "mg/dL",
                    "reference_interval": candidate["reference_interval"],
                    "abnormal_flag": "",
                    "provenance": "imported",
                    "confirmer": str(rbac_graph.physician),
                    "page": candidate["region"]["page"],
                    "bbox": candidate["region"]["bbox"],
                    "raw_text": candidate["raw_text"],
                    "extraction_candidate": str(row.pk),
                }
            ).encode(),
        )
        assert authorize_native(
            invocation_id=job.invocation_id,
            clinic_id=job.clinic_id,
            purpose=Purpose.EXTRACTION,
        )
        with (
            pytest.raises(DatabaseError) as refused,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT * FROM clinic_app.ai_review_extraction"
                "(%s,%s,'confirmed',%s,%s)",
                [
                    row.pk,
                    job.result_version_id,
                    source,
                    secret_store().get_secret("tenant-kek"),
                ],
            )
        assert getattr(refused.value.__cause__, "sqlstate", None) == "23514"
        assert not ResultObservation.objects.filter(
            version_id=job.result_version_id
        ).exists()


@pytest.mark.django_db(transaction=True)
def test_reviewed_image_ocr_still_confirms_its_bound_unit(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apps.ai.adapters.synthetic_document_extraction", recorded_document_extraction
    )
    job = extracted_job(
        rbac_graph, monkeypatch, data=scan_fixture(), content_type="image/png"
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.get(position=0)
        assert row.payload["unit"] == "mg/dL"
        identifier = review_extraction(
            clinic_id=job.clinic_id, candidate_id=row.pk, decision="confirmed"
        )
        assert identifier is not None
        assert ResultObservation.objects.get(pk=identifier).source["unit"] == "mg/dL"
