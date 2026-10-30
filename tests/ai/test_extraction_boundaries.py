"""Real source, adapter, parsing and current-authority extraction boundaries."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import extraction
from apps.ai.adapters import RecordedFixtureProvider, SyntheticProvider
from apps.ai.contracts import GatewayInputError
from apps.ai.extraction_contracts import ExtractionInputError, Region, parse_proposals
from apps.ai.extraction_documents import (
    MAX_DOCUMENT_BYTES,
    document_input,
    document_pages,
    render_page,
)
from apps.ai.output_validation import call_provider
from django.db import connection
from PIL import Image

from ai.test_extraction import pdf_fixture
from ai.test_extraction_runtime import extracted_job
from ai.test_units import request
from ehr.test_autosave import as_actor

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


@pytest.mark.parametrize(
    "content", ["not-json", "[]", "{}", '{"candidates":[]}', '{"candidates":null}']
)
def test_malformed_proposals_cannot_create_a_candidate(content: str) -> None:
    with pytest.raises(ExtractionInputError):
        parse_proposals(content, rotations=(0,))


@pytest.mark.parametrize("rotations", [(), (45,), (0, 360)])
def test_source_rotation_is_never_repaired(rotations: tuple[int, ...]) -> None:
    with pytest.raises(ExtractionInputError):
        parse_proposals('{"candidates":[]}', rotations=rotations)
    with pytest.raises(ExtractionInputError):
        Region(1, (0, 0, 100, 100)).display_bbox(45)


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"", "application/pdf"),
        (b"not-a-pdf", "application/pdf"),
        (b"%PDF-1.4\n%%EOF", "application/pdf"),
        (b"synthetic", "text/plain"),
    ],
)
def test_invalid_documents_do_not_produce_geometry(
    data: bytes, content_type: str
) -> None:
    with pytest.raises(ExtractionInputError):
        document_pages(data=data, content_type=content_type)


def test_document_size_and_requested_page_are_bounded() -> None:
    with pytest.raises(ExtractionInputError):
        document_pages(
            data=b"x" * (MAX_DOCUMENT_BYTES + 1), content_type="application/pdf"
        )
    for page in (0, 2, True):
        with pytest.raises(ExtractionInputError):
            render_page(data=pdf_fixture(), content_type="application/pdf", page=page)


def test_png_rasterization_retains_pixels_and_refuses_disguised_gif() -> None:
    source = BytesIO()
    Image.new("RGB", (12, 8), (11, 22, 33)).save(source, format="PNG")
    rendered = render_page(data=source.getvalue(), content_type="image/png", page=1)
    with Image.open(BytesIO(rendered)) as image:
        assert image.format == "PNG"
        assert image.size == (12, 8)
        assert image.getpixel((0, 0)) == (11, 22, 33)
    disguised = BytesIO()
    Image.new("RGB", (12, 8), "white").save(disguised, format="GIF")
    with pytest.raises(ExtractionInputError):
        document_pages(data=disguised.getvalue(), content_type="image/png")


def test_synthetic_adapter_refuses_a_malformed_document_envelope() -> None:
    query = replace(
        request(),
        content='{"schema":"document-extraction-v1","malformed":true}',
    )
    with pytest.raises(GatewayInputError):
        call_provider(SyntheticProvider(), request=query, method="extract")


def test_recorded_adapter_keeps_exact_scan_identity_and_local_parsing() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[2] / "apps/ai/fixtures/document_extraction_v1.json"
        ).read_text()
    )
    query = replace(
        request(),
        endpoint="https://recorded.synthetic.invalid/v1/inference",
        model_id="extraction-fixture-v1",
        content=document_input(
            data=base64.b64decode(fixture["data_base64"], validate=True),
            content_type="image/png",
        ),
    )
    result = call_provider(RecordedFixtureProvider(), request=query, method="extract")
    candidates = parse_proposals(json.loads(result.content)["text"], rotations=(0,))
    assert [row.parsed_value for row in candidates] == ["123.40", "12.50"]
    with pytest.raises(GatewayInputError):
        call_provider(
            RecordedFixtureProvider(),
            request=replace(
                query,
                content=document_input(
                    data=pdf_fixture(), content_type="application/pdf"
                ),
            ),
            method="extract",
        )


@pytest.mark.django_db(transaction=True)
def test_patient_scope_and_expiry_are_decided_at_the_actual_boundary(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    assert job.result_version_id is not None
    with as_actor(rbac_graph, rbac_graph.physician):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.has_permission('clinical.read',%s,NULL),"
                "clinic_app.has_permission('clinical.read',%s,%s)",
                [job.clinic_id, job.clinic_id, job.patient_enrollment_id],
            )
            assert cursor.fetchone() == (False, True)
        candidate = job.candidates.order_by("position").first()
        assert candidate is not None
        with pytest.raises(ExtractionInputError):
            extraction.extraction_source(clinic_id=job.clinic_id)
        with pytest.raises(ExtractionInputError):
            extraction.extraction_source(
                clinic_id=job.clinic_id,
                result_version_id=job.result_version_id,
                attachment_id=uuid4(),
            )
        with pytest.raises(ExtractionInputError):
            extraction.review_extraction(
                clinic_id=job.clinic_id,
                candidate_id=candidate.pk,
                decision="approved",
            )
        monkeypatch.setattr(
            "apps.ai.extraction.timezone.now", lambda: candidate.expires_at
        )
        with pytest.raises(ExtractionInputError):
            extraction.review_extraction(
                clinic_id=job.clinic_id,
                candidate_id=candidate.pk,
                decision="confirmed",
            )
