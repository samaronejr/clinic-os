"""Supersede an import without deleting anything it wrote.

Rollback appends the terminal ``rolled_back`` transition (a fresh step-up
session and, after commit, the signed rollback window are enforced by the
database guard), then asks the database to hide every imported enrollment
that nothing else references yet. Enrollments already in use (a booking, an
encounter, a message, a demographics correction...) are retained and listed
for review; imported clinical records stay immutable and become invisible
because their job is superseded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from apps.interop.access import (
    Code,
    ImportInputError,
    hide_reversible,
    import_authority,
    import_step_up,
    record,
)
from apps.interop.jobs import ImportJobView, append, expect, job_view
from apps.interop.models import JOB_STATES, ROLLBACK_REASONS, ImportJob
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class RollbackReceipt:
    """What the rollback hid, and which in-use enrollments it retained."""

    job: ImportJobView
    hidden: int
    retained_enrollment_ids: tuple[UUID, ...]


def rollback_import(
    *,
    clinic_id: UUID,
    job_id: UUID,
    expected_sequence: int,
    reason: str,
    session_key: str,
) -> RollbackReceipt:
    """Mark the job superseded and hide its still-unused enrollments."""
    if reason not in ROLLBACK_REASONS:
        raise ImportInputError(Code.INVALID_REASON)
    with transaction.atomic():
        actor = import_authority(clinic_id=clinic_id)
        # The clinic gate orders this decision against bookings and batches.
        acquire_advisory_locks((clinic_lock_key(clinic_id),))
        import_step_up(session_key=session_key)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        latest = expect(
            job,
            expected_sequence=expected_sequence,
            states=tuple(state for state in JOB_STATES if state != "rolled_back"),
        )
        transition = append(
            job, latest, state="rolled_back", actor_id=actor, reason=reason
        )
        outcomes = hide_reversible(job.pk)
        return RollbackReceipt(
            job=job_view(job, transition),
            hidden=sum(1 for _enrollment, hidden in outcomes if hidden),
            retained_enrollment_ids=tuple(
                enrollment for enrollment, hidden in outcomes if not hidden
            ),
        )
