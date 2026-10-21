"""Bind future note creation to the retained bundle and psychiatry restriction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.ehr.note_models import RestrictedNote
from apps.ehr.pack_models import SpecialtyPack
from apps.ehr.services import ClinicalConflictError

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ehr.models import ClinicalDocument, SpecialtyTemplate


def bind_pack_note(
    *, clinic_id: UUID, template: SpecialtyTemplate, document: ClinicalDocument
) -> None:
    """Bind the restriction before the database serializes new bundle use."""
    pack = SpecialtyPack.objects.filter(clinic_id=clinic_id, template=template).first()
    if pack is None:
        return
    if pack.uninstalled_at is not None:
        reason = "pack_retired"
        raise ClinicalConflictError(reason)
    if pack.key == "psychiatry":
        note, created = RestrictedNote.objects.get_or_create(
            document=document,
            defaults={
                "organization_id": document.organization_id,
                "clinic_id": clinic_id,
                "restricted_by_id": document.encounter.physician_id,
            },
        )
        if created:
            record_phase1_event(
                "ehr.note.restricted", clinic_id=clinic_id, affected_record_id=note.pk
            )
