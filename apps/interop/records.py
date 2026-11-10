"""Read imported clinical history with its ``imported`` provenance intact.

Imported notes, observations and documents are never attributed to a
current user: each item carries the source system's own author label and
time, and the label ``imported``. Reads require current patient-scoped
clinical access; history of a superseded (rolled-back) import is hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.interop.access import (
    Code,
    ImportAccessDeniedError,
    ImportConflictError,
    attachment_envelope,
    clinical_reader,
    history_envelopes,
    opened,
)
from apps.interop.models import ImportedRecord, ImportFile, ImportTransition
from apps.interop.sources import sha256_hex

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

PROVENANCE: Final = "imported"
HISTORY_KINDS: Final = ("note", "observation", "attachment")


@dataclass(frozen=True, slots=True)
class ImportedHistoryItem:
    """One immutable imported fact as the source system recorded it."""

    record_id: UUID
    job_id: UUID
    kind: str
    original_author: str
    original_recorded_at: datetime | None
    content: dict[str, object]
    provenance: str = PROVENANCE


@dataclass(frozen=True, slots=True)
class ImportedAttachment:
    """Verified attachment bytes of one imported document."""

    record_id: UUID
    media_type: str
    sha256: str
    data: bytes


def _superseded(job_ids: set[UUID]) -> set[UUID]:
    return set(
        ImportTransition.objects.filter(
            job_id__in=job_ids, state="rolled_back"
        ).values_list("job_id", flat=True)
    )


def imported_history(
    *, clinic_id: UUID, enrollment_id: UUID
) -> tuple[ImportedHistoryItem, ...]:
    """List one patient's imported history, oldest original time first."""
    with transaction.atomic():
        clinical_reader(clinic_id=clinic_id, enrollment_id=enrollment_id)
        rows = history_envelopes(clinic_id=clinic_id, enrollment_id=enrollment_id)
        hidden = _superseded({row.job_id for row in rows})
        items = tuple(
            ImportedHistoryItem(
                record_id=row.record_id,
                job_id=row.job_id,
                kind=row.kind,
                original_author=opened(
                    ImportedRecord, "original_author", row.original_author
                ),
                original_recorded_at=row.original_recorded_at,
                content=cast(
                    "dict[str, object]", opened(ImportedRecord, "content", row.content)
                ),
            )
            for row in rows
            if row.kind in HISTORY_KINDS and row.job_id not in hidden
        )
        record_phase1_event(
            "interop.records.viewed",
            clinic_id=clinic_id,
            affected_record_id=enrollment_id,
        )
        return items


def imported_attachment(*, clinic_id: UUID, record_id: UUID) -> ImportedAttachment:
    """Return one imported document's bytes after re-verifying its checksum."""
    with transaction.atomic():
        stored = attachment_envelope(clinic_id=clinic_id, record_id=record_id)
        clinical_reader(clinic_id=clinic_id, enrollment_id=stored.enrollment_id)
        if _superseded({stored.job_id}):
            raise ImportAccessDeniedError
        data = bytes(opened(ImportFile, "data", stored.data))
        if sha256_hex(data) != stored.sha256:
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        record_phase1_event(
            "interop.records.viewed",
            clinic_id=clinic_id,
            affected_record_id=stored.enrollment_id,
        )
        return ImportedAttachment(
            record_id=stored.record_id,
            media_type=stored.media_type,
            sha256=stored.sha256,
            data=data,
        )
