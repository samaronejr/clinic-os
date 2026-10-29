"""Review-bound prior-note reuse through the existing CAS and section-epoch writer."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import record_phase1_event
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.note_access import _author
from apps.ehr.note_models import CopiedNoteBlock, RestrictedNote
from apps.ehr.services import (
    SOAP_FIELDS,
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    decided,
    record_clinical_note,
)

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class CopyNoteCommand:
    """Bind the reviewed destination revision, section and replay identity."""

    section: str
    expected_revision: int
    command_id: UUID


def _copy_scope(
    *, clinic_id: UUID, target_version_id: UUID, source_version_id: UUID, section: str
) -> tuple[ClinicalDocumentVersion, ClinicalDocumentVersion]:
    """Authorize both the target and the currently readable same-patient source."""
    target = _author(clinic_id=clinic_id, version_id=target_version_id)
    source = (
        ClinicalDocumentVersion.objects.filter(
            pk=source_version_id,
            document__encounter__clinic_id=clinic_id,
            document__encounter__patient_id=target.document.encounter.patient_id,
            state__in=("finalized", "superseded"),
        )
        .select_related("document__encounter__clinic")
        .first()
    )
    if (
        source is None
        or source.document.encounter.patient_id != target.document.encounter.patient_id
        or section not in SOAP_FIELDS
        or source.pk == target.pk
    ):
        raise ClinicalAccessDeniedError
    return target, source


def reuse_prior_note(
    *,
    clinic_id: UUID,
    target_version_id: UUID,
    source_version_id: UUID,
    command: CopyNoteCommand,
) -> CopiedNoteBlock:
    """Copy a currently readable same-patient source, never an unreviewed final."""
    section = command.section
    expected_revision = command.expected_revision
    command_id = command.command_id

    authority = partial(
        _copy_scope,
        clinic_id=clinic_id,
        target_version_id=target_version_id,
        source_version_id=source_version_id,
        section=section,
    )
    authority()
    with decided(authority):
        target, source = authority()
        locked = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(pk=target.pk)
            .first()
        )
        if locked is None:
            raise ClinicalAccessDeniedError
        target = locked
        receipt = CopiedNoteBlock.objects.filter(
            target=target, command_id=command_id
        ).first()
        if receipt is not None:
            if (
                receipt.source_id != source.pk
                or receipt.section != section
                or receipt.inserted_revision != expected_revision + 1
            ):
                reason = "idempotency_mismatch"
                raise ClinicalConflictError(reason)
            return receipt
        if target.state != "draft":
            reason = "precondition_failed"
            raise ClinicalConflictError(reason)
        if target.revision != expected_revision:
            reason = "stale_revision"
            raise ClinicalConflictError(reason)
        if RestrictedNote.objects.filter(
            document_id=source.document_id
        ).exists() and not (
            RestrictedNote.objects.filter(document_id=target.document_id).exists()
        ):
            reason = "restricted_source"
            raise ClinicalConflictError(reason)
        source_time = source.finalized_at
        if source_time is None:
            raise ClinicalAccessDeniedError
        zone_name = source.document.encounter.clinic.timezone
        if zone_name is None:
            raise ClinicalAccessDeniedError
        date = timezone.localtime(source_time, ZoneInfo(zone_name)).strftime("%d/%m/%Y")
        marker = _("Copied from %(date)s") % {"date": date}
        content = target.soap
        copied = f"{marker}\n{source.soap[section]}"
        content[section] = f"{content[section]}\n\n{copied}".strip()
        saved = record_clinical_note(
            clinic_id=clinic_id,
            version_id=target.pk,
            expected_revision=expected_revision,
            content=content,
        )
        receipt = CopiedNoteBlock.objects.create(
            organization_id=target.organization_id,
            clinic_id=clinic_id,
            target=target,
            source=source,
            section=section,
            command_id=command_id,
            inserted_revision=saved.revision,
        )
        record_phase1_event(
            "ehr.prior_note.copied", clinic_id=clinic_id, affected_record_id=receipt.pk
        )
        return receipt


def review_copied_block(
    *, clinic_id: UUID, block_id: UUID, expected_revision: int
) -> CopiedNoteBlock:
    """Acknowledge the exact draft revision after the author reviews the copy."""
    block = CopiedNoteBlock.objects.filter(pk=block_id, clinic_id=clinic_id).first()
    if block is None:
        raise ClinicalAccessDeniedError
    authority = partial(_author, clinic_id=clinic_id, version_id=block.target_id)
    authority()
    with decided(authority):
        target = (
            ClinicalDocumentVersion.objects.select_for_update()
            .filter(pk=block.target_id)
            .first()
        )
        if target is None:
            raise ClinicalAccessDeniedError
        if target.state != "draft" or target.revision != expected_revision:
            reason = "stale_revision"
            raise ClinicalConflictError(reason)
        row = CopiedNoteBlock.objects.select_for_update().filter(pk=block.pk).first()
        if row is None:
            raise ClinicalAccessDeniedError
        if row.reviewed_revision == target.revision:
            return row
        row.reviewed_revision = target.revision
        row.reviewed_by_id = target.author_id
        row.reviewed_at = timezone.now()
        row.save(update_fields=("reviewed_revision", "reviewed_by", "reviewed_at"))
        record_phase1_event(
            "ehr.prior_note.reviewed", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row


def require_copy_review(version: ClinicalDocumentVersion) -> None:
    """Every edit invalidates earlier review; finalization never confirms a copy."""
    if version.copied_blocks.exclude(
        reviewed_revision=version.revision, reviewed_by_id=version.author_id
    ).exists():
        reason = "copied_blocks_unreviewed"
        raise ClinicalConflictError(reason)
