"""Source-scoped extraction admission, materialization, and explicit human review."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.ai.artifacts import create_input, read_artifact
from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayRefusedError,
    Purpose,
)
from apps.ai.extraction_contracts import (
    ExtractionCandidate,
    ExtractionInputError,
    filing_payload,
    parse_proposals,
)
from apps.ai.extraction_documents import document_input, document_pages
from apps.ai.models import (
    AIArtifact,
    AIInvocation,
    ExtractionCandidateRecord,
    ExtractionJob,
)
from apps.ai.services import invoke
from apps.audit.services import record_phase1_event
from apps.core.secrets import secret_store
from apps.ehr.attachments import authorize_attachment_encounter, download_attachment
from apps.ehr.models import ClinicalAttachment
from apps.identity.current_context import current_actor_id, require_permission
from apps.intake.models import PatientClinicEnrollment
from apps.orders.access import current_version, require_version
from apps.orders.storage import source_bytes
from apps.tenancy.envelope import protect

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True, repr=False)
class ExtractionSource:
    """Ephemeral source bytes, authorized before decryption and never logged."""

    organization_id: UUID
    enrollment_id: UUID
    sha256: str
    content_type: str
    data: bytes = field(default=b"", repr=False)


def extraction_source(
    *,
    clinic_id: UUID,
    result_version_id: UUID | None = None,
    attachment_id: UUID | None = None,
    include_bytes: bool = False,
) -> ExtractionSource:
    """Resolve one exact source through its existing domain authority."""
    if (result_version_id is None) == (attachment_id is None):
        raise ExtractionInputError
    if result_version_id is not None:
        version = current_version(clinic_id=clinic_id, version_id=result_version_id)
        matched = require_version(
            clinic_id=clinic_id, version_id=result_version_id, permission="result.read"
        )
        require_permission(
            "clinical.read",
            clinic_id=clinic_id,
            patient_enrollment_id=matched.order.enrollment_id,
        )
        return ExtractionSource(
            version.organization_id,
            matched.order.enrollment_id,
            version.sha256,
            version.content_type,
            source_bytes(version) if include_bytes else b"",
        )
    attachment = ClinicalAttachment.objects.filter(
        pk=cast("UUID", attachment_id), clinic_id=clinic_id, state="available"
    ).first()
    if attachment is None:
        raise GatewayAccessDeniedError
    encounter = authorize_attachment_encounter(clinic_id, attachment.encounter_id)
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=encounter.patient_id
    ).first()
    if enrollment is None:
        raise GatewayAccessDeniedError
    require_permission(
        "clinical.read", clinic_id=clinic_id, patient_enrollment_id=enrollment.pk
    )
    data = (
        download_attachment(clinic_id=clinic_id, attachment_id=attachment.pk).data
        if include_bytes
        else b""
    )
    return ExtractionSource(
        attachment.organization_id,
        enrollment.pk,
        attachment.sha256,
        attachment.detected_type,
        data,
    )


@transaction.atomic
def start_extraction(
    *,
    clinic_id: UUID,
    budget: Budget,
    result_version_id: UUID | None = None,
    attachment_id: UUID | None = None,
) -> ExtractionJob:
    """Queue only through the disabled-by-default, budgeted gateway."""
    source = extraction_source(
        clinic_id=clinic_id,
        result_version_id=result_version_id,
        attachment_id=attachment_id,
        include_bytes=True,
    )
    content = document_input(data=source.data, content_type=source.content_type)
    artifact = create_input(
        clinic_id=clinic_id,
        purpose=Purpose.EXTRACTION,
        content=content,
        patient_enrollment_id=source.enrollment_id,
        method="extract",
    )
    admission = invoke(
        clinic_id=clinic_id,
        purpose=Purpose.EXTRACTION,
        principal=current_actor_id(),
        inputs_ref=artifact.pk,
        budget=budget,
    )
    if admission.reason is not None or admission.invocation_id is None:
        raise GatewayRefusedError(admission.reason or "configuration_unavailable")
    job = ExtractionJob.objects.create(
        organization_id=source.organization_id,
        clinic_id=clinic_id,
        user_id=current_actor_id(),
        patient_enrollment_id=source.enrollment_id,
        result_version_id=result_version_id,
        attachment_id=attachment_id,
        sha256=source.sha256,
        rotations=document_pages(data=source.data, content_type=source.content_type),
        invocation_id=admission.invocation_id,
    )
    record_phase1_event(
        "ai.extraction.started", clinic_id=clinic_id, affected_record_id=job.pk
    )
    return job


def authorized_job(*, clinic_id: UUID, job_id: UUID) -> ExtractionJob:
    """Recheck source authority for every display, rasterization, or review."""
    job = ExtractionJob.objects.filter(pk=job_id, clinic_id=clinic_id).first()
    if job is None:
        raise GatewayAccessDeniedError
    source = extraction_source(
        clinic_id=clinic_id,
        result_version_id=job.result_version_id,
        attachment_id=job.attachment_id,
    )
    if source.sha256 != job.sha256 or source.enrollment_id != job.patient_enrollment_id:
        raise GatewayAccessDeniedError
    return job


@transaction.atomic
def collect_extraction(*, clinic_id: UUID, job_id: UUID) -> ExtractionJob:
    """Materialize an invocation once; inference never acknowledges or files results."""
    job = authorized_job(clinic_id=clinic_id, job_id=job_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ai-extraction:{job.pk}"],
        )
    if job.candidates.exists():
        return job
    invocation = AIInvocation.objects.filter(pk=job.invocation_id).first()
    if invocation is None:
        raise GatewayAccessDeniedError
    if invocation.status != "succeeded":
        return job
    artifact = AIArtifact.objects.filter(
        invocation_id=job.invocation_id, kind="output"
    ).first()
    if artifact is None:
        raise GatewayAccessDeniedError
    output = json.loads(read_artifact(clinic_id=clinic_id, artifact_id=artifact.pk))
    candidates = parse_proposals(output["text"], rotations=tuple(job.rotations))
    for position, candidate in enumerate(candidates):
        row = ExtractionCandidateRecord.objects.create(
            organization_id=job.organization_id,
            clinic_id=clinic_id,
            job=job,
            position=position,
            payload=candidate.payload(),
            status=candidate.status,
            expires_at=artifact.expires_at,
        )
        record_phase1_event(
            "ai.extraction.proposed", clinic_id=clinic_id, affected_record_id=row.pk
        )
    return job


def candidate_data(row: ExtractionCandidateRecord) -> ExtractionCandidate:
    """Reparse the encrypted draft instead of trusting its stored parsed value."""
    payload = row.payload
    proposal = {
        key: payload[key]
        for key in ("field", "kind", "raw_text", "value", "unit", "reference_interval")
    }
    proposal.update(
        page=payload["region"]["page"],
        bbox=payload["region"]["bbox"],
        legible=payload["reason"] != "unreadable",
    )
    candidate = parse_proposals(
        json.dumps({"candidates": [proposal]}), rotations=tuple(row.job.rotations)
    )[0]
    if candidate.payload() != payload:
        raise ExtractionInputError
    return candidate


@transaction.atomic
def review_extraction(
    *,
    clinic_id: UUID,
    candidate_id: UUID,
    decision: str,
    result_version_id: UUID | None = None,
) -> UUID | None:
    """Recheck identity, authority, expiry, and one effect in the resolver."""
    row = (
        ExtractionCandidateRecord.objects.select_related("job")
        .filter(pk=candidate_id, clinic_id=clinic_id)
        .first()
    )
    if row is None:
        raise GatewayAccessDeniedError
    job = authorized_job(clinic_id=clinic_id, job_id=row.job_id)
    actor = require_permission(
        "clinical.write",
        clinic_id=clinic_id,
        patient_enrollment_id=job.patient_enrollment_id,
    )
    if decision not in ("confirmed", "rejected") or row.expires_at <= timezone.now():
        raise ExtractionInputError
    target = result_version_id or job.result_version_id
    source = b""
    if decision == "confirmed":
        if target is None:
            raise ExtractionInputError
        version = current_version(clinic_id=clinic_id, version_id=target)
        matched = require_version(
            clinic_id=clinic_id, version_id=target, permission="result.read"
        )
        if (
            version.sha256 != job.sha256
            or matched.order.enrollment_id != job.patient_enrollment_id
        ):
            raise GatewayAccessDeniedError
        candidate = candidate_data(row)
        payload = filing_payload(candidate, confirmer=str(actor))
        payload["extraction_candidate"] = str(row.pk)
        source = protect(
            purpose="orders.observation.source",
            plaintext=json.dumps(payload, separators=(",", ":")).encode(),
        )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.ai_review_extraction(%s,%s,%s,%s,%s)",
                [
                    candidate_id,
                    target,
                    decision,
                    source,
                    secret_store().get_secret("tenant-kek"),
                ],
            )
            receipt = cursor.fetchone()
    except DatabaseError as error:
        if getattr(error.__cause__, "sqlstate", None) in ("23514", "40001", "42501"):
            raise GatewayAccessDeniedError from None
        raise
    if receipt is None:
        raise GatewayAccessDeniedError
    if receipt[1]:
        record_phase1_event(
            f"ai.extraction.{decision}", clinic_id=clinic_id, affected_record_id=row.pk
        )
    return cast("UUID | None", receipt[0])
