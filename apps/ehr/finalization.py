"""Finalization, amendment, discard and closure under the record contract.

Finalized content is immutable: the version row carries a fixed SHA-256
content digest computed over the canonical SOAP fields, and database triggers
reject every later mutation. Corrections happen only through a linked
amendment draft that supersedes its base at finalization. Local finalization
is not a provider-verified digital signature; signing is a later capability.
"""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import rfc8785
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.ehr.services import (
    SOAP_FIELDS,
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _appointment,
    _assigned,
    _denied,
    record_denial,
    view_version,
)
from apps.identity.stepup import StepUpRequired, assert_step_up
from apps.prescription.models import PrescriptionDraft

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

    from apps.ehr.models import ClinicalDocument

MAX_REASON = 255


def content_digest(version: ClinicalDocumentVersion) -> str:
    """Hash the exact retained template binding and canonical SOAP content."""
    return sha256(
        rfc8785.dumps(
            {
                "v": "ehr-document-v1",
                "template_id": str(version.template_id),
                "template_version": version.template.version,
                "subjective": version.subjective,
                "objective": version.objective,
                "assessment": version.assessment,
                "plan": version.plan,
            }
        )
    ).hexdigest()


def _next_version(document: ClinicalDocument) -> int:
    """Allocate the next version through the resolver, past hidden discards."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_next_version(%s)", [document.pk])
        row = cursor.fetchone()
    if row is None:
        raise ClinicalAccessDeniedError
    return int(row[0])


def _author_version(clinic_id: UUID, version_id: UUID) -> ClinicalDocumentVersion:
    """Fetch the version for a write transition without a read audit event.

    Row security already limits visibility to the author's own drafts and
    discarded versions plus assigned/care-readable finalized content; this
    adds the assigned-physician and authorship predicates for writes.
    """
    version = (
        ClinicalDocumentVersion.objects.filter(
            pk=version_id, document__encounter__clinic_id=clinic_id
        )
        .select_related("document__encounter")
        .first()
    )
    if version is None:
        _denied(clinic_id, version_id, "no_care_relationship")
    actor = _assigned(
        _appointment(clinic_id, version.document.encounter.appointment_id)
    )
    if version.author_id != actor:
        _denied(clinic_id, version_id, "not_assigned")
    return version


def finalize_version(
    *,
    clinic_id: UUID,
    version_id: UUID,
    expected_revision: int,
    request: HttpRequest,
) -> ClinicalDocumentVersion:
    """Freeze one draft with a fixed digest; retries return the first result.

    Recent step-up verification is a service precondition, not a caller
    convention: ``request`` must carry the author's verified OTP device and a
    fresh ``otp_verified_at`` timestamp, exactly as the HTTP boundary
    produces. Missing, stale or foreign verification is denied
    ``step_up_required`` and audited before any state is read for update.
    """
    authorized = _author_version(clinic_id, version_id)
    try:
        assert_step_up(request)
    except StepUpRequired:
        record_denial(clinic_id, authorized.pk, "step_up_required")
        raise
    if request.user.pk != authorized.author_id:
        record_denial(clinic_id, authorized.pk, "step_up_required")
        raise StepUpRequired
    with transaction.atomic():
        version = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(pk=version_id)
            .first()
        )
        if version is None:
            # A superseded row is visible but outside the UPDATE policy.
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        if version.state == "finalized":
            return version
        if version.state != "draft":
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        if version.revision != expected_revision:
            msg = "stale_revision"
            raise ClinicalConflictError(msg)
        if any(not getattr(version, field).strip() for field in SOAP_FIELDS):
            msg = "missing_required_content"
            raise ClinicalConflictError(msg)
        if version.amendment_of_id is not None:
            base = (
                ClinicalDocumentVersion.objects.select_for_update()
                .filter(pk=version.amendment_of_id)
                .first()
            )
            if base is None or base.state != "finalized":
                msg = "stale_revision"
                raise ClinicalConflictError(msg)
            base.state = "superseded"
            base.save(update_fields=("state",))
        version.state = "finalized"
        version.content_digest = content_digest(version)
        version.finalized_at = timezone.now()
        version.save(
            update_fields=("state", "content_digest", "finalized_at", "updated_at")
        )
        record_phase1_event(
            "ehr.document.finalized",
            clinic_id=clinic_id,
            affected_record_id=version.pk,
        )
        return version


def amend_document(
    *, clinic_id: UUID, version_id: UUID, reason: str
) -> ClinicalDocumentVersion:
    """Open one linked amendment draft on the document's current version."""
    authorized = view_version(clinic_id=clinic_id, version_id=version_id)
    actor = _assigned(
        _appointment(clinic_id, authorized.document.encounter.appointment_id)
    )
    reason = reason.strip()
    if not reason or len(reason) > MAX_REASON:
        msg = "Informe o motivo da retificação."
        raise ValidationError(msg)
    with transaction.atomic():
        current = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(document_id=authorized.document_id, state="finalized")
            .first()
        )
        if authorized.state == "draft":
            msg = "draft_in_progress"
            raise ClinicalConflictError(msg)
        if authorized.state != "finalized" or current is None:
            msg = "stale_revision"
            raise ClinicalConflictError(msg)
        if current.pk != authorized.pk:
            msg = "stale_revision"
            raise ClinicalConflictError(msg)
        if ClinicalDocumentVersion.objects.filter(
            document_id=authorized.document_id, state="draft"
        ).exists():
            msg = "draft_in_progress"
            raise ClinicalConflictError(msg)
        version = ClinicalDocumentVersion(
            organization_id=authorized.organization_id,
            document_id=authorized.document_id,
            template_id=authorized.template_id,
            author_id=actor,
            amendment_of=current,
            amendment_reason=reason,
            version=_next_version(authorized.document),
        )
        # The amendment draft carries the base version's exact content: the
        # stored plaintext digest is the trigger's equality proof.
        version.set_soap(current.soap)
        version.save()
        record_phase1_event(
            "ehr.document.amended",
            clinic_id=clinic_id,
            affected_record_id=version.pk,
        )
        return version


def discard_draft(*, clinic_id: UUID, version_id: UUID) -> ClinicalDocumentVersion:
    """Retire one draft without content; discarded versions are never served."""
    _author_version(clinic_id, version_id)
    with transaction.atomic():
        version = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(pk=version_id)
            .first()
        )
        if version is None or version.state != "draft":
            msg = "precondition_failed"
            raise ClinicalConflictError(msg)
        version.state = "discarded"
        version.save(update_fields=("state", "updated_at"))
        # The binding trigger archived the SOAP body and blanked the stored
        # row; refresh so the returned object is the metadata-only version.
        version.refresh_from_db()
        record_phase1_event(
            "ehr.document.discarded",
            clinic_id=clinic_id,
            affected_record_id=version.pk,
        )
        return version


def close_encounter(*, clinic_id: UUID, encounter_id: UUID) -> Encounter:
    """Close once every version is terminal; a retry returns the closed row."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    _assigned(_appointment(clinic_id, encounter.appointment_id))
    with transaction.atomic():
        encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
        if encounter.state == Encounter.State.CLOSED:
            return encounter
        if (
            ClinicalDocumentVersion.objects.filter(
                document__encounter=encounter, state="draft"
            ).exists()
            or PrescriptionDraft.objects.filter(
                encounter=encounter, state="draft"
            ).exists()
        ):
            msg = "draft_in_progress"
            raise ClinicalConflictError(msg)
        encounter.state = Encounter.State.CLOSED
        encounter.closed_at = timezone.now()
        encounter.save(update_fields=("state", "closed_at"))
        record_phase1_event(
            "ehr.encounter.closed",
            clinic_id=clinic_id,
            affected_record_id=encounter.pk,
        )
        return encounter
