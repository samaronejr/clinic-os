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
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.interop import contract
from apps.interop.access import (
    Code,
    ImportConflictError,
    attachment_checks,
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
from apps.interop.projection import mapped_values, summarize
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
    """What one batch did: committed, deferred, waiting for patients, or skipped."""

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
        # Each registration ends its audit critical section before another
        # patient can extend it; history appends only at batch end.
        plan: list[tuple[str, Sequence[PatientRow] | Sequence[HistoryRow]]] = [
            ("patients", (row,)) for row in normalized.patients
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
) -> tuple[tuple[str, UUID], ...]:
    audits: list[tuple[str, UUID]] = []
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
            source_record_id=row.original_source_id or row.source_id,
            original_author=row.registered_by,
            original_recorded_at=row.registered_at,
            content={"fields": sorted(k for k, v in row.canonical().items() if v)},
            content_sha256=_content_digest(row),
            identifier_count=imported.identifier_count,
        )
        audits.extend(imported.audit_events)
    return tuple(audits)


def _commit_history(
    *, clinic: Clinic, batch: ImportBatch, rows: Sequence[HistoryRow]
) -> int:
    patients = {
        item.row_number: item
        for item in ImportedRecord.objects.filter(
            job_id=batch.job_id,
            dataset="patients",
            row_number__in={row.patient_row for row in rows},
        ).only("row_number", "patient_id", "enrollment_id")
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
        if row.file and (
            stored is None
            or stored.scan_verdict != "clean"
            or stored.sha256 != row.content["sha256"]
            or stored.media_type != row.content["media_type"]
            or stored.size_bytes != row.content["size_bytes"]
        ):
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        target = ImportedRecord(
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
            source_record_id=row.original_source_id or row.source_id,
            original_author=row.author,
            original_recorded_at=row.recorded_at,
            content=dict(row.content),
        )
        # Bind the values actually supplied to the immutable target insert.
        # At reconciliation only the hash/structural metadata may be read.
        values = row.canonical()
        values["original_source_id" if row.original_source_id else "source_id"] = (
            target.source_record_id
        )
        values.update(
            kind=target.kind,
            recorded_at=(
                target.original_recorded_at.isoformat()
                if target.original_recorded_at
                else None
            ),
            author=target.original_author,
            content=target.content,
            file=stored.path if stored else None,
        )
        target.content_sha256 = hashlib.sha256(canonical_bytes(values)).hexdigest()
        target.save(force_insert=True)
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


def _try_audit_chain(*, clinic: Clinic) -> bool:
    """Try audit_append_unchecked_v2's exact organization-chain key."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_try_advisory_xact_lock("
            "pg_catalog.hashtextextended(('clinic-audit:' || %s::uuid::text)"
            ' COLLATE pg_catalog."C", 0::bigint))',
            [str(clinic.organization_id)],
        )
        return cursor.fetchone()[0] is True


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
        # Do not generate protected rows and WAL for an already-busy chain.
        # Roll back this empty probe so preparation cannot retain the pin.
        with transaction.atomic():
            available = _try_audit_chain(clinic=clinic)
            transaction.set_rollback(True)
        if not available:
            return BatchOutcome(batch_id=batch.pk, state="deferred", committed=0)
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
            audits: tuple[tuple[str, UUID], ...] = ()
            if batch.dataset == "patients":
                audits = _commit_patients(
                    clinic=clinic,
                    actor=actor,
                    batch=batch,
                    rows=cast("Sequence[PatientRow]", rows),
                )
                committed = len(rows)
            else:
                committed = _commit_history(
                    clinic=clinic, batch=batch, rows=cast("Sequence[HistoryRow]", rows)
                )
            ImportBatch.objects.filter(pk=batch.pk).update(
                state="committed", committed_count=committed
            )
            completion: tuple[dict[str, object], str] | None = None
            if not ImportBatch.objects.filter(job_id=job.pk, state="pending").exists():
                report = reconciliation(job, normalized, approval=latest)
                completion = report, contract.document_digest(report)
            # A clinical writer may have arrived during preparation.
            # Re-check without waiting and roll back if that race was lost.
            if not _try_audit_chain(clinic=clinic):
                transaction.set_rollback(True)
                return BatchOutcome(batch_id=batch.pk, state="deferred", committed=0)
            for event_type, record_id in audits:
                record_phase1_event(
                    event_type, clinic_id=clinic.pk, affected_record_id=record_id
                )
            record_phase1_event(
                "interop.batch.committed",
                clinic_id=clinic_id,
                affected_record_id=batch.pk,
            )
            # The chain pin orders revocation. Authority lost during
            # preparation rolls back all rows and ordered audit events.
            identity_authority(clinic_id=clinic.pk)
            if completion is not None:
                report, digest = completion
                append(
                    job,
                    latest,
                    state="committed",
                    actor_id=actor,
                    mapping=latest.mapping,
                    report=report,
                    digest=digest,
                )
        return BatchOutcome(batch_id=batch.pk, state="committed", committed=committed)


def _matches_approved_counts_and_rejects(
    job: ImportJob, normalized: Normalized, approval: ImportTransition
) -> bool:
    if (
        normalized.row_count != approval.row_count
        or normalized.valid_count != approval.valid_count
        or len(normalized.rejections) != approval.rejected_count
    ):
        return False
    if not normalized.rejections:
        return True
    # The batch hashes bind accepted rows; only the approval binds rejects.
    report = approval.report
    if not isinstance(report, dict):
        return False
    plan = report.get("plan")
    return (
        isinstance(plan, dict)
        and plan.get("rejects_acknowledged") is True
        and report.get("dry_run_digest")
        == dataset_digest_for(job, cast("ImportMapping", approval.mapping), normalized)
    )


def reconciliation(
    job: ImportJob, normalized: Normalized, *, approval: ImportTransition
) -> dict[str, object]:
    """Compare source, mapped and committed counts, values and attachment bytes."""
    target = list(
        ImportedRecord.objects.filter(job_id=job.pk).values_list(
            "dataset",
            "row_number",
            "content_sha256",
            "patient_id",
            "enrollment_id",
            "file__path",
        )
    )
    committed = {
        name: sum(item[0] == name for item in target) for name in normalized.row_counts
    }
    accepted = {
        "patients": len(normalized.patients),
        "history": len(normalized.history),
    }
    rejected = Counter(item.dataset for item in normalized.rejections)
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
    linked, attachment_bytes, mismatches = attachment_checks(job.pk)
    source_verified = True
    try:
        source_bytes(job)
    except ImportConflictError:
        source_verified = False
    datasets = {
        name: {
            "rows": normalized.row_counts[name],
            "accepted": accepted[name],
            "rejected": rejected[name],
            "committed": committed[name],
        }
        for name in normalized.row_counts
    }
    target_patients = {
        row: (patient, enrollment)
        for dataset, row, _, patient, enrollment, _ in target
        if dataset == "patients"
    }
    expected_records = {
        ("patients", row.row): (_content_digest(row), None, None)
        for row in normalized.patients
    } | {
        ("history", row.row): (
            _content_digest(row),
            target_patients.get(row.patient_row),
            row.file,
        )
        for row in normalized.history
    }
    actual_records = {
        (dataset, row): (
            digest,
            (patient, enrollment) if dataset == "history" else None,
            file or None,
        )
        for dataset, row, digest, patient, enrollment, file in target
    }
    committed_values_verified = (
        len(expected_records) == len(target) == normalized.valid_count
        and expected_records == actual_records
    )
    positions = actual_records.keys() | {
        (item.dataset, item.row) for item in normalized.rejections
    }
    accounted_count = len(target) + len(normalized.rejections)
    # The parser numbers each dataset's counted rows contiguously from one.
    rows_accounted = len(positions) == accounted_count == normalized.row_count and all(
        1 <= row <= normalized.row_counts.get(dataset, 0) for dataset, row in positions
    )
    source = normalized.source_projection
    mapped = summarize(mapped_values(normalized)) if source is not None else None
    values: dict[str, object] = {"committed_values_verified": committed_values_verified}
    if source is not None:
        values.update(
            v5_source=source, mapped=mapped, source_projection_matches=source == mapped
        )
    expected_files = {
        row.file: row.content["size_bytes"]
        for row in normalized.history
        if row.file is not None
    }
    reconciled = (
        rows_accounted
        and _matches_approved_counts_and_rejects(job, normalized, approval)
        and source_verified
        and mismatches == 0
        and linked == len(expected_files)
        and attachment_bytes
        == sum(int(value) for value in expected_files.values() if value is not None)
        and batches_verified
        and committed_values_verified
        and source == mapped
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
            "attachments_verified": linked - mismatches,
            "attachment_mismatches": mismatches,
            "batches_verified": batches_verified,
        },
        "canonical_values": values,
    }


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
