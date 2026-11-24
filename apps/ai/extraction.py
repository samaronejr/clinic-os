"""Source-scoped extraction admission, materialization, and explicit human review."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
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
from apps.ai.effects import check_invocation
from apps.ai.extraction_contracts import (
    ExtractionCandidate,
    ExtractionInputError,
    PdfValueEvidence,
    filing_payload,
    parse_proposals,
)
from apps.ai.extraction_documents import document_input, document_pages, source_regions
from apps.ai.models import (
    AIArtifact,
    AIInvocation,
    ExtractionCandidateRecord,
    ExtractionJob,
)
from apps.ai.pending_choice import pending_choice_allows
from apps.ai.pending_outputs import (
    PendingKey,
    ai_pending_visible,
    decisions_visible,
    key,
)
from apps.ai.pending_outputs import rows as pending_rows
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

    from apps.ai.extraction_contracts import SourceEvidence
    from apps.ai.pending_outputs import RenderSelection, VisiblePending


@dataclass(frozen=True, slots=True, repr=False)
class ExtractionSource:
    """Ephemeral source bytes, authorized before decryption and never logged."""

    organization_id: UUID
    enrollment_id: UUID
    sha256: str
    content_type: str
    data: bytes = field(default=b"", repr=False)

    def regions(self, *, rotations: tuple[int, ...]) -> SourceEvidence:
        """Load local review evidence from this authorized source."""
        return source_regions(
            data=self.data, content_type=self.content_type, rotations=rotations
        )


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
    job = pending_rows(ExtractionJob).create(
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
    job = pending_rows(ExtractionJob).filter(pk=job_id, clinic_id=clinic_id).first()
    if job is None:
        raise GatewayAccessDeniedError
    try:
        check_invocation(clinic_id=clinic_id, invocation_id=job.invocation_id)
    except GatewayRefusedError:
        raise GatewayAccessDeniedError from None
    source = job_source(job=job, include_bytes=False)
    if source.sha256 != job.sha256 or source.enrollment_id != job.patient_enrollment_id:
        raise GatewayAccessDeniedError
    if not pending_choice_allows(
        clinic_id=clinic_id,
        enrollment_id=source.enrollment_id,
        generated_at=job.created_at,
    ):
        raise GatewayAccessDeniedError
    return job


def job_source(*, job: ExtractionJob, include_bytes: bool) -> ExtractionSource:
    """Resolve the job's exact immutable source under current read authority."""
    return extraction_source(
        clinic_id=job.clinic_id,
        result_version_id=job.result_version_id,
        attachment_id=job.attachment_id,
        include_bytes=include_bytes,
    )


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
    artifact = (
        pending_rows(AIArtifact)
        .filter(invocation_id=job.invocation_id, kind="output")
        .first()
    )
    if artifact is None:
        raise GatewayAccessDeniedError
    output = json.loads(read_artifact(clinic_id=clinic_id, artifact_id=artifact.pk))
    rotations = tuple(job.rotations)
    regions = job_source(job=job, include_bytes=True).regions(rotations=rotations)
    candidates = parse_proposals(output["text"], rotations=rotations, regions=regions)
    for position, candidate in enumerate(candidates):
        row = pending_rows(ExtractionCandidateRecord).create(
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


def candidate_data(
    selector: PendingKey[ExtractionCandidateRecord],
    *,
    clinic_id: UUID,
    job_id: UUID,
) -> ExtractionCandidate:
    """Resolve a candidate for the filing command using the same checked scope."""
    job_key = PendingKey(ExtractionJob, job_id)

    def project(batch: RenderSelection) -> ExtractionCandidate:
        job, candidate = batch.get(job_key), batch.get(selector)
        if job is None or candidate is None or candidate.record.job_id != job_id:
            raise GatewayAccessDeniedError
        rotations = tuple(job.record.rotations)
        regions = job_source(job=job.record, include_bytes=True).regions(
            rotations=rotations
        )
        return project_candidate(candidate, rotations=rotations, regions=regions)

    return ai_pending_visible((selector, job_key), project, clinic_id=clinic_id)


def project_candidate(
    checked: VisiblePending[ExtractionCandidateRecord],
    *,
    rotations: tuple[int, ...],
    regions: SourceEvidence,
) -> ExtractionCandidate:
    """Reparse checked ciphertext instead of trusting the stored parsed value."""
    row = checked.record
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
        json.dumps({"candidates": [proposal]}), rotations=rotations, regions=regions
    )[0]
    if candidate.payload() != payload:
        if isinstance(regions, PdfValueEvidence):
            # Legacy PDF rows remain immutable; do not display their old unit
            # or treat the old candidate as eligible for the new command.
            return replace(
                candidate,
                status="ambiguous",
                parsed_value="",
                reason="value_unresolved",
            )
        raise ExtractionInputError
    return candidate


@transaction.atomic
def review_extraction(
    *,
    clinic_id: UUID,
    candidate_id: UUID,
    decision: str,
    result_version_id: UUID | None = None,
    manual_unit: str | None = None,
) -> UUID | None:
    """Recheck identity, authority, expiry, and one effect in the resolver."""
    row = (
        pending_rows(ExtractionCandidateRecord)
        .select_related("job")
        .filter(pk=candidate_id, clinic_id=clinic_id)
        .first()
    )
    if row is None or not decisions_visible((key(row),), clinic_id=clinic_id):
        raise GatewayAccessDeniedError
    job = authorized_job(clinic_id=clinic_id, job_id=row.job_id)
    actor = require_permission(
        "clinical.write",
        clinic_id=clinic_id,
        patient_enrollment_id=job.patient_enrollment_id,
    )
    if (
        decision not in ("confirmed", "rejected")
        or row.expires_at <= timezone.now()
        or (decision == "rejected" and manual_unit is not None)
    ):
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
        candidate = candidate_data(key(row), clinic_id=clinic_id, job_id=job.pk)
        payload = filing_payload(
            candidate, confirmer=str(actor), manual_unit=manual_unit
        )
        payload["extraction_candidate"] = str(row.pk)
        source = protect(
            purpose="orders.observation.source",
            plaintext=json.dumps(payload, separators=(",", ":")).encode(),
        )
    check_invocation(clinic_id=clinic_id, invocation_id=job.invocation_id)
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
