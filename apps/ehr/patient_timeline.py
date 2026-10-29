"""Alias-aware clinical metadata, preserving every original record binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.ehr.models import ClinicalDocumentVersion
from apps.intake.access import authorized_enrollment_for
from apps.intake.alias_access import patient_alias_set

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    """A retained document's original identity and immutable provenance."""

    encounter_id: UUID
    document_id: UUID
    version_id: UUID
    patient_id: UUID
    version: int
    state: str
    content_digest: str
    finalized_at: datetime | None


@dataclass(frozen=True, slots=True)
class PatientTimeline:
    """Unified projection; no historical foreign key is rewritten."""

    survivor_patient_id: UUID
    unified_at: datetime | None
    records: tuple[TimelineRecord, ...]


def patient_record_timeline(*, clinic_id: UUID, enrollment_id: UUID) -> PatientTimeline:
    """Read only care-authorized metadata through the existing clinical RLS."""
    _actor, _clinic, enrollment = authorized_enrollment_for(
        clinic_id, enrollment_id, "clinical.read"
    )
    aliases = patient_alias_set(clinic_id=clinic_id, patient_id=enrollment.patient_id)
    versions = (
        ClinicalDocumentVersion.objects.filter(
            document__encounter__clinic_id=clinic_id,
            document__encounter__patient_id__in=aliases.patient_ids,
            state__in=("finalized", "superseded"),
        )
        .select_related("document__encounter")
        .only(
            "id",
            "document_id",
            "version",
            "state",
            "content_digest",
            "finalized_at",
            "document__id",
            "document__encounter_id",
            "document__encounter__id",
            "document__encounter__patient_id",
            "document__encounter__clinic_id",
        )
        .order_by("finalized_at", "id")
    )
    records = tuple(
        TimelineRecord(
            row.document.encounter_id,
            row.document_id,
            row.pk,
            row.document.encounter.patient_id,
            row.version,
            row.state,
            row.content_digest,
            row.finalized_at,
        )
        for row in versions
    )
    for record in records:
        record_phase1_event(
            "ehr.record.viewed",
            clinic_id=clinic_id,
            affected_record_id=record.version_id,
        )
    return PatientTimeline(aliases.survivor_patient_id, aliases.unified_at, records)
