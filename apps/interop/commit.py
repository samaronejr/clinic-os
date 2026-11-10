"""Commit an approved import in bounded batches and reconcile what landed.

``commit_import`` requires the operator's fresh step-up session, re-derives
the dry-run digest from the stored source and refuses any drift, then
records immutable batch bounds. Each batch runs on the ``bulk`` queue under
the requester's re-checked authority, re-verifies its own row digest and
writes imported records whose provenance keeps the original source ids,
author and time. The last batch records the reconciliation report.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.interop import contract
from apps.interop.access import (
    Code,
    ImportConflictError,
    identity_authority,
    import_authority,
    import_clinic,
    import_step_up,
    record,
    translated,
)
from apps.interop.jobs import (
    ImportJobView,
    append,
    dataset_digest_for,
    expect,
    job_view,
    latest_transition,
    mapped_rows,
    source_bytes,
)
from apps.interop.models import (
    ImportBatch,
    ImportedRecord,
    ImportFile,
    ImportJob,
    ImportMapping,
    ImportTransition,
)
from apps.interop.patients import register
from apps.interop.sources import sha256_hex
from apps.interop.validation import (
    HistoryRow,
    PatientRow,
    canonical_bytes,
    rows_digest,
)
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from apps.identity.models import Clinic
    from apps.interop.validation import Normalized

BATCH_ROWS: Final = 200


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """The immutable batch plan a commit queued on the bulk queue."""

    job: ImportJobView
    batch_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """What one batch run did: committed, waiting for patients, or skipped."""

    batch_id: UUID
    state: str
    committed: int


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Counts, bytes and checksums of one committed import."""

    job: ImportJobView
    reconciled: bool
    digest: str
    report: dict[str, object]


def _chunks[T](rows: Sequence[T]) -> list[Sequence[T]]:
    return [
        rows[index : index + BATCH_ROWS] for index in range(0, len(rows), BATCH_ROWS)
    ]


def _signed_dry_run(job: ImportJob) -> str:
    approved = (
        ImportTransition.objects.filter(job_id=job.pk, state="approved")
        .order_by("-sequence")
        .first()
    )
    report = approved.report if approved is not None else None
    if not isinstance(report, dict) or not isinstance(
        report.get("dry_run_digest"), str
    ):
        raise ImportConflictError(Code.INTEROP_TRANSITION)
    return str(report["dry_run_digest"])


def _dispatch(batch_ids: tuple[UUID, ...]) -> None:
    from apps.interop.tasks import commit_batch_job  # noqa: PLC0415 - task cycle

    for batch_id in batch_ids:
        commit_batch_job.apply_async(kwargs={"batch_id": str(batch_id)})


def commit_import(
    *, clinic_id: UUID, job_id: UUID, expected_sequence: int, session_key: str
) -> CommitReceipt:
    """Start the approved commit once; an equal replay returns the same plan."""
    with transaction.atomic():
        actor = identity_authority(clinic_id=clinic_id)
        clinic = import_clinic(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        latest = expect(job, expected_sequence=expected_sequence, states=("approved",))
        import_step_up(session_key=session_key)
        existing = tuple(
            ImportBatch.objects.filter(job_id=job.pk)
            .order_by("sequence")
            .values_list("pk", flat=True)
        )
        if existing:
            return CommitReceipt(job=job_view(job, latest), batch_ids=existing)
        mapping = cast("ImportMapping", latest.mapping)
        normalized = mapped_rows(job, clinic=clinic, mapping=mapping)
        if dataset_digest_for(job, mapping, normalized) != _signed_dry_run(job):
            raise ImportConflictError(Code.DRY_RUN_STALE)
        plan: list[tuple[str, Sequence[PatientRow] | Sequence[HistoryRow]]] = [
            ("patients", chunk) for chunk in _chunks(normalized.patients)
        ]
        plan += [("history", chunk) for chunk in _chunks(normalized.history)]
        created: list[UUID] = []
        with translated(), transaction.atomic():
            for sequence, (dataset, rows) in enumerate(plan, start=1):
                batch = ImportBatch.objects.create(
                    organization_id=job.organization_id,
                    clinic_id=job.clinic_id,
                    job=job,
                    sequence=sequence,
                    dataset=dataset,
                    first_row=rows[0].row,
                    last_row=rows[-1].row,
                    rows_sha256=rows_digest([row.canonical() for row in rows]),
                    requested_by_id=actor,
                )
                created.append(batch.pk)
        batch_ids = tuple(created)
        transaction.on_commit(lambda: _dispatch(batch_ids))
        return CommitReceipt(job=job_view(job, latest), batch_ids=batch_ids)


def _content_digest(row: PatientRow | HistoryRow) -> str:
    return hashlib.sha256(canonical_bytes(row.canonical())).hexdigest()


def _commit_patients(
    *, clinic: Clinic, actor: UUID, batch: ImportBatch, rows: Sequence[PatientRow]
) -> int:
    for row in rows:
        imported = register(clinic=clinic, actor_id=actor, job_id=batch.job_id, row=row)
        ImportedRecord.objects.create(
            organization_id=clinic.organization_id,
            clinic=clinic,
            job_id=batch.job_id,
            batch=batch,
            dataset="patients",
            row_number=row.row,
            kind="patient",
            patient_id=imported.patient_id,
            enrollment_id=imported.enrollment_id,
            source_record_id=row.source_id,
            original_author=row.registered_by,
            original_recorded_at=row.registered_at,
            content={"fields": sorted(k for k, v in row.canonical().items() if v)},
            content_sha256=_content_digest(row),
            identifier_count=imported.identifier_count,
        )
    return len(rows)


def _commit_history(
    *, clinic: Clinic, batch: ImportBatch, rows: Sequence[HistoryRow]
) -> int:
    patients = {
        item.row_number: item
        for item in ImportedRecord.objects.filter(
            job_id=batch.job_id,
            dataset="patients",
            row_number__in={row.patient_row for row in rows},
        )
    }
    files = {
        str(item.path): item
        for item in ImportFile.objects.filter(
            job_id=batch.job_id, role="attachment"
        ).defer("data")
    }
    for row in rows:
        patient = patients[row.patient_row]
        stored = files.get(row.file) if row.file else None
        if row.file and (stored is None or stored.sha256 != row.content.get("sha256")):
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        ImportedRecord.objects.create(
            organization_id=clinic.organization_id,
            clinic=clinic,
            job_id=batch.job_id,
            batch=batch,
            dataset="history",
            row_number=row.row,
            kind=row.kind,
            patient_id=patient.patient_id,
            enrollment_id=patient.enrollment_id,
            file=stored,
            source_record_id=row.source_id,
            original_author=row.author,
            original_recorded_at=row.recorded_at,
            content=dict(row.content),
            content_sha256=_content_digest(row),
        )
    return len(rows)


def _batch_rows(
    normalized: Normalized, batch: ImportBatch
) -> Sequence[PatientRow] | Sequence[HistoryRow]:
    if batch.dataset == "patients":
        return [
            row
            for row in normalized.patients
            if batch.first_row <= row.row <= batch.last_row
        ]
    return [
        row
        for row in normalized.history
        if batch.first_row <= row.row <= batch.last_row
    ]


def run_batch(*, clinic_id: UUID, batch_id: UUID) -> BatchOutcome:
    """Commit one pending batch exactly once under re-checked current authority."""
    with transaction.atomic():
        actor = identity_authority(clinic_id=clinic_id)
        # The clinic gate orders this batch against bookings and rollback.
        acquire_advisory_locks((clinic_lock_key(clinic_id),))
        clinic = import_clinic(clinic_id=clinic_id)
        batch = record(ImportBatch, clinic_id=clinic_id, record_id=batch_id)
        job = batch.job
        latest = latest_transition(job)
        if latest.state != "approved" or batch.state != "pending":
            return BatchOutcome(batch_id=batch.pk, state="skipped", committed=0)
        if (
            batch.dataset == "history"
            and ImportBatch.objects.filter(
                job_id=job.pk, dataset="patients", state="pending"
            ).exists()
        ):
            return BatchOutcome(batch_id=batch.pk, state="waiting", committed=0)
        normalized = mapped_rows(
            job, clinic=clinic, mapping=cast("ImportMapping", latest.mapping)
        )
        rows = _batch_rows(normalized, batch)
        if (
            not rows
            or rows_digest([row.canonical() for row in rows]) != batch.rows_sha256
        ):
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        with translated(), transaction.atomic():
            if batch.dataset == "patients":
                committed = _commit_patients(
                    clinic=clinic,
                    actor=actor,
                    batch=batch,
                    rows=cast("Sequence[PatientRow]", rows),
                )
            else:
                committed = _commit_history(
                    clinic=clinic, batch=batch, rows=cast("Sequence[HistoryRow]", rows)
                )
            ImportBatch.objects.filter(pk=batch.pk).update(
                state="committed", committed_count=committed
            )
        record_phase1_event(
            "interop.batch.committed", clinic_id=clinic_id, affected_record_id=batch.pk
        )
        if not ImportBatch.objects.filter(job_id=job.pk, state="pending").exists():
            _finish(job, latest, actor=actor, normalized=normalized)
        return BatchOutcome(batch_id=batch.pk, state="committed", committed=committed)


def reconciliation(job: ImportJob, normalized: Normalized) -> dict[str, object]:
    """Compare accepted and committed counts, bytes and every stored checksum."""
    committed = {
        name: ImportedRecord.objects.filter(job_id=job.pk, dataset=name).count()
        for name in normalized.row_counts
    }
    accepted = {
        "patients": len(normalized.patients),
        "history": len(normalized.history),
    }
    batches = []
    batches_verified = True
    for batch in ImportBatch.objects.filter(job_id=job.pk).order_by("sequence"):
        rows = _batch_rows(normalized, batch)
        verified = rows_digest([row.canonical() for row in rows]) == batch.rows_sha256
        batches_verified = (
            batches_verified and verified and (batch.committed_count == len(rows))
        )
        batches.append(
            {
                "sequence": batch.sequence,
                "dataset": batch.dataset,
                "rows": len(rows),
                "committed": batch.committed_count,
                "rows_sha256": batch.rows_sha256,
                "verified": verified,
            }
        )
    linked = ImportFile.objects.filter(
        job_id=job.pk, role="attachment", importedrecord__isnull=False
    ).distinct()
    attachment_bytes = 0
    mismatches = 0
    for item in linked:
        data = bytes(item.data)
        attachment_bytes += len(data)
        if sha256_hex(data) != item.sha256 or len(data) != item.size_bytes:
            mismatches += 1
    source_verified = True
    try:
        source_bytes(job)
    except ImportConflictError:
        source_verified = False
    datasets = {
        name: {
            "rows": normalized.row_counts[name],
            "accepted": accepted[name],
            "rejected": normalized.row_counts[name] - accepted[name],
            "committed": committed[name],
        }
        for name in normalized.row_counts
    }
    reconciled = (
        all(entry["accepted"] == entry["committed"] for entry in datasets.values())
        and source_verified
        and mismatches == 0
        and batches_verified
    )
    return {
        "schema": "import-reconciliation-v1",
        "reconciled": reconciled,
        "datasets": datasets,
        "batches": batches,
        "bytes": {"source": job.size_bytes, "attachments": attachment_bytes},
        "checksums": {
            "source_sha256": job.source_sha256,
            "source_verified": source_verified,
            "attachments_verified": len(linked) - mismatches,
            "attachment_mismatches": mismatches,
            "batches_verified": batches_verified,
        },
    }


def _finish(
    job: ImportJob, latest: ImportTransition, *, actor: UUID, normalized: Normalized
) -> None:
    report = reconciliation(job, normalized)
    append(
        job,
        latest,
        state="committed",
        actor_id=actor,
        mapping=latest.mapping,
        report=report,
        digest=contract.document_digest(report),
    )


def reconciliation_report(*, clinic_id: UUID, job_id: UUID) -> ReconciliationReport:
    """Return the reconciliation recorded when the last batch committed."""
    with transaction.atomic():
        import_authority(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        committed = (
            ImportTransition.objects.filter(job_id=job.pk, state="committed")
            .order_by("-sequence")
            .first()
        )
        if committed is None or not isinstance(committed.report, dict):
            raise ImportConflictError(Code.INTEROP_TRANSITION)
        report = cast("dict[str, object]", committed.report)
        return ReconciliationReport(
            job=job_view(job),
            reconciled=report.get("reconciled") is True,
            digest=committed.digest,
            report=report,
        )
