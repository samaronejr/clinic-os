"""Independent client/organization reads of pending AI, before and after refusal."""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

import pytest
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.extraction import candidate_data, collect_extraction, start_extraction
from apps.ai.pending_outputs import key
from apps.consent.services import record_ai_disclosure
from apps.core.integration import execute_operation
from apps.ehr.attachments import AttachmentInput, scan_attachment, upload_attachment
from apps.ehr.models import Encounter
from apps.intake.models import PatientClinicEnrollment
from django.urls import reverse

from ai import care_support
from ai.chart_consent_support import accept_ai
from ai.pending_display_support import withdraw
from ai.support import DEFAULT_BUDGET
from ai.test_care_views import _client
from ai.test_extraction_filing import matching_submission
from ai.test_extraction_runtime import extracted_job
from core.test_navigation import _post
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from interop.support import org_admin
from interop.test_org_export import (
    _archive,
    _build,
    _files,
    _jsonl,
    _receipt,
    _request,
    dispatched,
)
from orders.support import ordered
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

__all__ = ("dispatched",)
pytestmark = pytest.mark.django_db(transaction=True)


def _fresh_archive(g: RbacGraph, admin: UUID) -> dict[str, bytes]:
    job = _request(g, admin)
    assert _build(g, admin, job) == "ready"
    return _files(_archive(g, admin, _receipt(g, admin, job.export_id)))


def _pending_care_ids(files: dict[str, bytes], fmt: str) -> set[str]:
    path = "data/ai_careproposal." + fmt
    if fmt == "jsonl":
        return {str(r["id"]) for r in _jsonl(files[path]) if r["state"] == "pending"}
    records = csv.DictReader(io.StringIO(files[path].decode()))
    return {r["id"] for r in records if r["state"] == "pending"}


def test_held_extraction_candidate_is_reselected_before_projection(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = rbac_graph
    source = ordered(g)
    patient, receipt = accept_ai(g, source.enrollment)
    job = extracted_job(g, monkeypatch, world=source)
    with as_actor(g, g.physician):
        candidate = job.candidates.select_related("job").get(position=0)
        assert (
            candidate_data(
                key(candidate),
                clinic_id=g.clinic_a,
                job_id=job.pk,
            ).parsed_value
            == "123.40"
        )
    withdraw(patient, receipt)
    with as_actor(g, g.physician), pytest.raises(GatewayAccessDeniedError):
        candidate_data(
            key(candidate),
            clinic_id=g.clinic_a,
            job_id=job.pk,
        )


@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_fresh_download_omits_all_three_refused_care_proposals(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    dispatched: list[dict[str, object]],
    fmt: str,
) -> None:
    g = rbac_graph
    care = care_support.care_world(g, monkeypatch, synthetic_secret_backend)
    accept_ai(g, care.plan.enrollment_id)
    bundle = care_support.request_and_collect(care)
    keys = {str(i.pk) for i in bundle.items if i.state == "pending"}
    assert len(keys) == 3
    admin = org_admin(g)
    assert keys <= _pending_care_ids(_fresh_archive(g, admin), fmt)
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=care.plan.encounter_id,
            informed=True,
            refused=True,
        )
    # New job, new bytes, through the authorized digest-checking part reader.
    assert not keys.intersection(_pending_care_ids(_fresh_archive(g, admin), fmt))


@pytest.mark.parametrize("fragment", [False, True], ids=["native", "htmx"])
def test_attachment_refresh_does_not_offer_refused_pending_candidate(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fragment: bool,
) -> None:
    g = rbac_graph
    monkeypatch.setattr(
        "django.conf.settings.EHR_ATTACHMENT_ROOT", tmp_path / "scanned"
    )
    source = ordered(g)
    accept_ai(g, source.enrollment)
    previous = extracted_job(g, monkeypatch, world=source)
    submission = matching_submission(previous)
    with owner_context(g.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            pk=previous.patient_enrollment_id
        )
        encounter = Encounter.objects.get(
            patient_id=enrollment.patient_id,
            clinic_id=g.clinic_a,
            physician_id=g.physician,
        )
    with as_actor(g, g.physician):
        pdf = upload_attachment(
            clinic_id=g.clinic_a,
            encounter_id=encounter.pk,
            upload=AttachmentInput(
                file_name="synthetic.pdf",
                declared_type="application/pdf",
                data=submission.data,
            ),
        )
        scan_attachment(clinic_id=g.clinic_a, attachment_id=pdf.pk)
        job = start_extraction(
            clinic_id=g.clinic_a, attachment_id=pdf.pk, budget=DEFAULT_BUDGET
        )
        op = job.invocation.operation_id
    assert op is not None
    with runtime_role():
        assert execute_operation(operation_id=op) == "succeeded"
    with as_actor(g, g.physician):
        candidate = (
            collect_extraction(clinic_id=g.clinic_a, job_id=job.pk)
            .candidates.order_by("position")
            .first()
        )
        assert candidate is not None
    client = _client(g.physician, "physician")
    url = reverse("ai:extraction", args=(g.clinic_a,))
    form = {"action": "refresh", "job_id": str(job.pk)}
    client.defaults["HTTP_HX_REQUEST"] = "true" if fragment else "false"
    before = _post(client, url, form)
    assert before.status_code == 200
    assert str(candidate.pk).encode() in before.content
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a, encounter_id=encounter.pk, informed=True, refused=True
        )
    after = _post(client, url, form)
    assert str(candidate.pk).encode() not in after.content
