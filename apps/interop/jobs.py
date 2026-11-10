"""Upload, profile, map, dry-run and approve one governed import job.

Every command is keyword-only, clinic-scoped and re-authorized at the
database: the import authority, the job's current state and the expected
sequence (one append-only transition per command) are checked again by the
interop guard trigger, so a stale or replayed command is a conflict and
never a second effect.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import rfc8785
from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.intake.demographics import IDENTIFIER_INDEX_PURPOSE, _required_fields
from apps.intake.duplicate_detection import (
    NAME_BIRTH_PURPOSE,
    NAME_PREFIX_LENGTH,
    _current_identifiers,
    _current_indexes,
)
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_name_index import normalized_name_bytes
from apps.interop import contract, sources
from apps.interop.access import (
    Code,
    ImportConflictError,
    ImportInputError,
    blind_index_sets,
    identity_authority,
    import_authority,
    import_clinic,
    record,
    translated,
)
from apps.interop.models import (
    ImportFile,
    ImportJob,
    ImportMapping,
    ImportTransition,
)
from apps.interop.spreadsheet import write_csv
from apps.interop.validation import (
    AttachmentFacts,
    Normalized,
    dataset_digest,
    normalize,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date, datetime

    from apps.identity.models import Clinic
    from apps.interop.validation import PatientRow

SHA256: Final = re.compile(r"[0-9a-f]{64}")
CODE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
SOURCE_MEDIA: Final = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "api": "application/json",
}
DRY_RUN_REPORT: Final = "import-dry-run-v1"
MAX_ROLLBACK_WINDOW_HOURS: Final = 720


@dataclass(frozen=True, slots=True)
class ImportJobView:
    """Metadata-only projection of one job and its current transition."""

    job_id: UUID
    source_kind: str
    size_bytes: int
    source_sha256: str
    state: str
    sequence: int
    mapping_id: UUID | None
    row_count: int
    valid_count: int
    rejected_count: int
    duplicate_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ImportMappingView:
    """One saved mapping version and its canonical digest."""

    mapping_id: UUID
    code: str
    version: int
    source_kind: str
    document_sha256: str
    suggested: bool


@dataclass(frozen=True, slots=True)
class ImportProfile:
    """A profile of the source structure; it carries no patient values."""

    job: ImportJobView
    profile: dict[str, object]


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """Counts, row-level reasons and duplicate hints bound by one digest."""

    job: ImportJobView
    digest: str
    report: dict[str, object]


@dataclass(frozen=True, slots=True)
class CutoverPlan:
    """The signed-off cutover and rollback plan an approval binds to."""

    rollback_window_hours: int
    source_frozen: bool
    backup_verified: bool
    rejects_acknowledged: bool
    duplicates_acknowledged: bool


def latest_transition(job: ImportJob) -> ImportTransition:
    """Return the current state row; every job is born with ``uploaded``."""
    latest = (
        ImportTransition.objects.filter(job_id=job.pk).order_by("-sequence").first()
    )
    if latest is None:
        raise ImportConflictError(Code.INTEROP_TRANSITION)
    return latest


def job_view(job: ImportJob, latest: ImportTransition | None = None) -> ImportJobView:
    """Project one job with its current transition."""
    current = latest or latest_transition(job)
    return ImportJobView(
        job_id=job.pk,
        source_kind=job.source_kind,
        size_bytes=job.size_bytes,
        source_sha256=job.source_sha256,
        state=current.state,
        sequence=current.sequence,
        mapping_id=current.mapping_id,
        row_count=current.row_count,
        valid_count=current.valid_count,
        rejected_count=current.rejected_count,
        duplicate_count=current.duplicate_count,
        created_at=job.created_at,
    )


def expect(
    job: ImportJob, *, expected_sequence: int, states: tuple[str, ...]
) -> ImportTransition:
    """Compare the caller's observed sequence and the allowed source states."""
    if type(expected_sequence) is not int:
        raise ImportInputError(Code.INVALID_COMMAND)
    latest = latest_transition(job)
    if latest.sequence != expected_sequence:
        raise ImportConflictError(Code.INTEROP_STALE)
    if latest.state not in states:
        raise ImportConflictError(Code.INTEROP_TRANSITION)
    return latest


def append(  # noqa: PLR0913 - one closed transition record
    job: ImportJob,
    latest: ImportTransition,
    *,
    state: str,
    actor_id: UUID,
    mapping: ImportMapping | None = None,
    report: Mapping[str, object] | None = None,
    digest: str = "",
    counts: tuple[int, int, int, int] | None = None,
    rollback_window_hours: int | None = None,
    reason: str = "",
) -> ImportTransition:
    """Append the next state; the guard rechecks authority, edge and payload."""
    row_count, valid_count, rejected_count, duplicate_count = counts or (
        latest.row_count,
        latest.valid_count,
        latest.rejected_count,
        latest.duplicate_count,
    )
    with translated(), transaction.atomic():
        transition = ImportTransition.objects.create(
            organization_id=job.organization_id,
            clinic_id=job.clinic_id,
            job=job,
            sequence=latest.sequence + 1,
            state=state,
            actor_id=actor_id,
            mapping=mapping,
            report=dict(report) if report is not None else None,
            digest=digest,
            row_count=row_count,
            valid_count=valid_count,
            rejected_count=rejected_count,
            duplicate_count=duplicate_count,
            rollback_window_hours=rollback_window_hours,
            reason=reason,
        )
    record_phase1_event(
        f"interop.job.{state}", clinic_id=job.clinic_id, affected_record_id=job.pk
    )
    return transition


def source_bytes(job: ImportJob) -> bytes:
    """Return the stored source after re-verifying its upload checksum."""
    stored = ImportFile.objects.filter(job_id=job.pk, role="source").first()
    data = bytes(stored.data) if stored is not None else b""
    if (
        stored is None
        or sources.sha256_hex(data) != job.source_sha256
        or stored.sha256 != job.source_sha256
        or len(data) != job.size_bytes
    ):
        raise ImportConflictError(Code.CHECKSUM_MISMATCH)
    return data


def attachment_facts(job: ImportJob) -> dict[str, AttachmentFacts]:
    """Quarantined archive members by path, without reading their bytes."""
    rows = ImportFile.objects.filter(job_id=job.pk, role="attachment").defer("data")
    return {
        str(row.path): AttachmentFacts(
            verdict=row.scan_verdict,
            sha256=row.sha256,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
        )
        for row in rows
    }


def csv_options(document: Mapping[str, object]) -> dict[str, sources.CsvOptions]:
    """Read the mapping's explicit text options for each CSV dataset."""
    raw = document.get("csv")
    if not isinstance(raw, dict):
        return {}
    return {
        name: sources.CsvOptions(
            delimiter=str(options["delimiter"]), encoding=str(options["encoding"])
        )
        for name, options in raw.items()
        if isinstance(options, dict)
    }


def mapped_rows(
    job: ImportJob, *, clinic: Clinic, mapping: ImportMapping
) -> Normalized:
    """Validate the stored source exactly as mapped; deterministic on replay."""
    document = cast("dict[str, object]", mapping.document)
    datasets = sources.read_datasets(
        job.source_kind, source_bytes(job), csv_options(document)
    )
    if not contract.mapped_columns_present(document, datasets):
        raise ImportInputError(Code.MAPPING_COLUMNS)
    return normalize(
        datasets,
        document,
        clinic=clinic,
        attachments=attachment_facts(job),
        required_fields=_required_fields(clinic.pk),
    )


def _fingerprint(source_kind: str, digest: str, size: int) -> str:
    return hashlib.sha256(
        rfc8785.dumps({"kind": source_kind, "sha256": digest, "size": size})
    ).hexdigest()


def upload_import(
    *,
    clinic_id: UUID,
    source_kind: str,
    data: bytes,
    declared_sha256: str,
    idempotency_key: UUID,
) -> ImportJobView:
    """Store one source after its checksum and structure verify; never parse twice."""
    if source_kind not in contract.SOURCE_KINDS:
        raise ImportInputError(Code.UNSUPPORTED_SOURCE)
    if type(data) is not bytes or not 1 <= len(data) <= sources.MAX_SOURCE_BYTES:
        raise ImportInputError(Code.SOURCE_SIZE)
    if type(idempotency_key) is not UUID:
        raise ImportInputError(Code.INVALID_COMMAND)
    if not isinstance(declared_sha256, str) or not SHA256.fullmatch(declared_sha256):
        raise ImportInputError(Code.INVALID_CHECKSUM)
    actual = sources.sha256_hex(data)
    if actual != declared_sha256:
        raise ImportConflictError(Code.CHECKSUM_MISMATCH)
    sources.read_datasets(source_kind, data)
    attachments = sources.archive_attachments(data) if source_kind == "zip" else ()
    fingerprint = _fingerprint(source_kind, actual, len(data))
    with transaction.atomic():
        actor = import_authority(clinic_id=clinic_id)
        clinic = import_clinic(clinic_id=clinic_id)
        existing = ImportJob.objects.filter(
            clinic_id=clinic_id, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise ImportConflictError(Code.IDEMPOTENCY_CONFLICT)
            return job_view(existing)
        with translated(), transaction.atomic():
            job = ImportJob.objects.create(
                organization_id=clinic.organization_id,
                clinic=clinic,
                source_kind=source_kind,
                size_bytes=len(data),
                source_sha256=actual,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                created_by_id=actor,
            )
            ImportFile.objects.create(
                organization_id=clinic.organization_id,
                clinic=clinic,
                job=job,
                role="source",
                path=None,
                sha256=actual,
                size_bytes=len(data),
                media_type=SOURCE_MEDIA[source_kind],
                scan_verdict="not_applicable",
                data=data,
            )
            for item in attachments:
                ImportFile.objects.create(
                    organization_id=clinic.organization_id,
                    clinic=clinic,
                    job=job,
                    role="attachment",
                    path=item.path,
                    sha256=item.sha256,
                    size_bytes=len(item.data),
                    media_type=item.media_type,
                    scan_verdict=item.verdict,
                    scan_reason=item.reason,
                    data=item.data,
                )
            first = ImportTransition.objects.create(
                organization_id=clinic.organization_id,
                clinic=clinic,
                job=job,
                sequence=1,
                state="uploaded",
                actor_id=actor,
                report={
                    "bytes": len(data),
                    "attachments": len(attachments),
                    "quarantined": sum(1 for a in attachments if a.verdict != "clean"),
                },
            )
        record_phase1_event(
            "interop.job.uploaded", clinic_id=clinic_id, affected_record_id=job.pk
        )
        return job_view(job, first)


def _cell_kind(value: str) -> str:
    text = value.strip()
    kind = "text"
    if not text:
        kind = "empty"
    elif re.fullmatch(r"-?[0-9]+", text):
        kind = "cpf_like" if len(re.sub(r"\D", "", text)) == 11 else "integer"  # noqa: PLR2004
    elif re.fullmatch(r"[0-9]{3}\.?[0-9]{3}\.?[0-9]{3}-?[0-9]{2}", text):
        kind = "cpf_like"
    elif re.fullmatch(r"-?[0-9]+[.,][0-9]+", text):
        kind = "decimal"
    elif re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        kind = "date"
    elif re.fullmatch(
        r"[0-9]{2}/[0-9]{2}/[0-9]{4} [0-9]{2}:[0-9]{2}"
        r"|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}",
        text,
    ):
        kind = "datetime"
    return kind


def _profile(
    job: ImportJob, datasets: Mapping[str, sources.Dataset], data: bytes
) -> dict[str, object]:
    files = sources.open_archive(data) if job.source_kind == "zip" else {}
    profiled: dict[str, object] = {}
    for name, dataset in datasets.items():
        columns = []
        for index, header in enumerate(dataset.headers):
            values = [row.cells[index] for row in dataset.rows if not row.defect]
            columns.append(
                {
                    "header": header,
                    "non_empty": sum(1 for value in values if value.strip()),
                    "max_length": max((len(value) for value in values), default=0),
                    "kinds": sorted({_cell_kind(value) for value in values}),
                }
            )
        entry: dict[str, object] = {
            "row_count": len(dataset.rows),
            "defective_rows": sum(1 for row in dataset.rows if row.defect),
            "columns": columns,
        }
        member = (
            data
            if job.source_kind == "csv"
            else files.get(sources.DATASET_FILES[name])
            if job.source_kind == "zip"
            else None
        )
        if member is not None:
            detected = sources.detect_csv(member)
            entry["csv"] = {
                "delimiter": detected.delimiter,
                "encoding": detected.encoding,
            }
        profiled[name] = entry
    stored = ImportFile.objects.filter(job_id=job.pk, role="attachment").defer("data")
    clean = sum(1 for row in stored if row.scan_verdict == "clean")
    count = len(stored)
    return {
        "schema": contract.PROFILE_NAME,
        "source_kind": job.source_kind,
        "datasets": profiled,
        "attachments": {
            "count": count,
            "clean": clean,
            "rejected": count - clean,
            "bytes": sum(row.size_bytes for row in stored),
        },
    }


def profile_import(
    *, clinic_id: UUID, job_id: UUID, expected_sequence: int
) -> ImportProfile:
    """Describe the source structure (headers, counts, kinds), never its values."""
    with transaction.atomic():
        actor = import_authority(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        latest = expect(job, expected_sequence=expected_sequence, states=("uploaded",))
        data = source_bytes(job)
        profile = _profile(job, sources.read_datasets(job.source_kind, data), data)
        if not contract.validate_profile(profile):
            raise ImportInputError(Code.INVALID_PROFILE)
        transition = append(
            job, latest, state="profiled", actor_id=actor, report=profile
        )
        return ImportProfile(job=job_view(job, transition), profile=profile)


def stored_profile(job: ImportJob) -> dict[str, object]:
    """Return the profile recorded by the job's ``profiled`` transition."""
    profiled = (
        ImportTransition.objects.filter(job_id=job.pk, state="profiled")
        .order_by("-sequence")
        .first()
    )
    if profiled is None or not isinstance(profiled.report, dict):
        raise ImportConflictError(Code.INTEROP_TRANSITION)
    return cast("dict[str, object]", profiled.report)


def suggest_mapping(*, clinic_id: UUID, job_id: UUID) -> contract.MappingSuggestion:
    """Draft an advisory mapping from profiled headers; nothing is saved."""
    with transaction.atomic():
        import_authority(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        profile = stored_profile(job)
        datasets = sources.read_datasets(job.source_kind, source_bytes(job))
        raw = profile.get("datasets")
        options = {
            name: entry["csv"]
            for name, entry in (raw.items() if isinstance(raw, dict) else ())
            if isinstance(entry, dict) and "csv" in entry
        }
        return contract.suggest(
            source_kind=job.source_kind, datasets=datasets, csv_options=options
        )


def mapping_view(mapping: ImportMapping) -> ImportMappingView:
    """Project one mapping version without its document."""
    return ImportMappingView(
        mapping_id=mapping.pk,
        code=mapping.code,
        version=mapping.version,
        source_kind=mapping.source_kind,
        document_sha256=mapping.document_sha256,
        suggested=mapping.suggested,
    )


def save_mapping(
    *,
    clinic_id: UUID,
    code: str,
    document: Mapping[str, object],
    suggested: bool = False,
) -> ImportMappingView:
    """Save the next immutable version of one clinic mapping."""
    if (
        not isinstance(code, str)
        or not CODE.fullmatch(code)
        or type(suggested) is not bool
    ):
        raise ImportInputError(Code.INVALID_COMMAND)
    validated = contract.validate_mapping(dict(document))
    with transaction.atomic():
        actor = import_authority(clinic_id=clinic_id)
        clinic = import_clinic(clinic_id=clinic_id)
        current = (
            ImportMapping.objects.filter(clinic_id=clinic_id, code=code)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        with translated(), transaction.atomic():
            mapping = ImportMapping.objects.create(
                organization_id=clinic.organization_id,
                clinic=clinic,
                code=code,
                version=(current or 0) + 1,
                source_kind=str(validated["source_kind"]),
                document=validated,
                document_sha256=contract.document_digest(validated),
                suggested=suggested,
                created_by_id=actor,
            )
        record_phase1_event(
            "interop.mapping.saved", clinic_id=clinic_id, affected_record_id=mapping.pk
        )
        return mapping_view(mapping)


def apply_mapping(
    *, clinic_id: UUID, job_id: UUID, mapping_id: UUID, expected_sequence: int
) -> ImportJobView:
    """Bind one saved mapping version whose columns exist in the source."""
    with transaction.atomic():
        actor = import_authority(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        latest = expect(
            job,
            expected_sequence=expected_sequence,
            states=("profiled", "mapped", "dry_run"),
        )
        mapping = record(ImportMapping, clinic_id=clinic_id, record_id=mapping_id)
        if mapping.source_kind != job.source_kind:
            raise ImportInputError(Code.MAPPING_SOURCE_KIND)
        document = cast("dict[str, object]", mapping.document)
        datasets = sources.read_datasets(
            job.source_kind, source_bytes(job), csv_options(document)
        )
        if not contract.mapped_columns_present(document, datasets):
            raise ImportInputError(Code.MAPPING_COLUMNS)
        transition = append(
            job,
            latest,
            state="mapped",
            actor_id=actor,
            mapping=mapping,
            counts=(0, 0, 0, 0),
        )
        return job_view(job, transition)


def existing_duplicates(
    clinic: Clinic, patients: tuple[PatientRow, ...]
) -> dict[int, list[str]]:
    """Exact-identifier and DOB+name-prefix hints against visible patients."""
    if not patients:
        return {}
    identifiers = [
        (row.row, kind, value) for row in patients for kind, value in row.identifiers
    ]
    identifier_digests = blind_index_sets(
        purpose=IDENTIFIER_INDEX_PURPOSE,
        plaintexts=[f"{kind}:{value}".encode() for _row, kind, value in identifiers],
    )
    named = [row for row in patients if row.birth_date is not None]
    name_digests = blind_index_sets(
        purpose=NAME_BIRTH_PURPOSE,
        plaintexts=[
            cast("date", row.birth_date).isoformat().encode("ascii")
            + b":"
            + normalized_name_bytes(row.legal_name or row.social_name or "")
            .decode("utf-8")[:NAME_PREFIX_LENGTH]
            .encode("utf-8")
            for row in named
        ],
    )
    known: dict[bytes, set[UUID]] = {}
    for identifier in _current_identifiers().filter(
        blind_index__in=[d for digests in identifier_digests for d in digests]
    ):
        known.setdefault(bytes(identifier.blind_index), set()).add(
            identifier.patient_id
        )
    names: dict[bytes, set[UUID]] = {}
    for index in _current_indexes().filter(
        name_birth_index__in=[d for digests in name_digests for d in digests]
    ):
        if index.name_birth_index is not None:
            names.setdefault(bytes(index.name_birth_index), set()).add(index.patient_id)
    matched = set().union(*known.values(), *names.values())
    # Hidden (rolled-back) enrollments are invisible here, so they never match.
    visible = set(
        PatientClinicEnrollment.objects.filter(
            clinic_id=clinic.pk, patient_id__in=matched
        ).values_list("patient_id", flat=True)
    )
    signals: dict[int, set[str]] = {}
    for (row, _kind, _value), digests in zip(
        identifiers, identifier_digests, strict=True
    ):
        if any(known.get(d, set()) & visible for d in digests):
            signals.setdefault(row, set()).add("exact_identifier")
    for patient, digests in zip(named, name_digests, strict=True):
        if any(names.get(d, set()) & visible for d in digests):
            signals.setdefault(patient.row, set()).add("dob+name_prefix")
    return {row: sorted(found) for row, found in sorted(signals.items())}


def dry_run_import(
    *, clinic_id: UUID, job_id: UUID, expected_sequence: int
) -> DryRunReport:
    """Validate every row, reconcile counts and route duplicates to review."""
    with transaction.atomic():
        actor = identity_authority(clinic_id=clinic_id)
        clinic = import_clinic(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        latest = expect(job, expected_sequence=expected_sequence, states=("mapped",))
        mapping = cast("ImportMapping", latest.mapping)
        normalized = mapped_rows(job, clinic=clinic, mapping=mapping)
        duplicates = existing_duplicates(clinic, normalized.patients)
        digest = dataset_digest_for(job, mapping, normalized)
        report = dry_run_document(normalized, duplicates)
        transition = append(
            job,
            latest,
            state="dry_run",
            actor_id=actor,
            mapping=mapping,
            report=report,
            digest=digest,
            counts=(
                normalized.row_count,
                normalized.valid_count,
                len(normalized.rejections),
                len(duplicates),
            ),
        )
        return DryRunReport(job=job_view(job, transition), digest=digest, report=report)


def dataset_digest_for(
    job: ImportJob, mapping: ImportMapping, normalized: Normalized
) -> str:
    """Bind the source, mapping, accepted rows and rejections into one digest."""
    return dataset_digest(
        normalized,
        source_sha256=job.source_sha256,
        mapping_sha256=mapping.document_sha256,
    )


def dry_run_document(
    normalized: Normalized, duplicates: Mapping[int, list[str]]
) -> dict[str, object]:
    """Report counts that reconcile: rows = accepted + rejected per dataset."""
    datasets: dict[str, dict[str, int]] = {}
    for name, rows in normalized.row_counts.items():
        accepted = len(
            normalized.patients if name == "patients" else normalized.history
        )
        datasets[name] = {
            "rows": rows,
            "accepted": accepted,
            "rejected": sum(
                1 for item in normalized.rejections if item.dataset == name
            ),
        }
    return {
        "schema": DRY_RUN_REPORT,
        "datasets": datasets,
        "rejections": [
            [item.dataset, item.row, item.field, item.code, item.source_id]
            for item in normalized.rejections
        ],
        "duplicates": [[row, signals] for row, signals in duplicates.items()],
    }


def approve_import(
    *, clinic_id: UUID, job_id: UUID, expected_sequence: int, plan: CutoverPlan
) -> ImportJobView:
    """Sign off the cutover and rollback plan bound to the exact dry run."""
    if (
        not isinstance(plan, CutoverPlan)
        or type(plan.rollback_window_hours) is not int
        or not 1 <= plan.rollback_window_hours <= MAX_ROLLBACK_WINDOW_HOURS
        or plan.source_frozen is not True
        or plan.backup_verified is not True
        or type(plan.rejects_acknowledged) is not bool
        or type(plan.duplicates_acknowledged) is not bool
    ):
        raise ImportInputError(Code.INVALID_PLAN)
    with transaction.atomic():
        actor = import_authority(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        latest = expect(job, expected_sequence=expected_sequence, states=("dry_run",))
        if latest.rejected_count and not plan.rejects_acknowledged:
            raise ImportConflictError(Code.REJECTS_UNACKNOWLEDGED)
        if latest.duplicate_count and not plan.duplicates_acknowledged:
            raise ImportConflictError(Code.DUPLICATES_UNACKNOWLEDGED)
        if not latest.valid_count:
            raise ImportConflictError(Code.NOTHING_TO_COMMIT)
        mapping = cast("ImportMapping", latest.mapping)
        signed = {
            "plan": {
                "rollback_window_hours": plan.rollback_window_hours,
                "source_frozen": plan.source_frozen,
                "backup_verified": plan.backup_verified,
                "rejects_acknowledged": plan.rejects_acknowledged,
                "duplicates_acknowledged": plan.duplicates_acknowledged,
            },
            "dry_run_digest": latest.digest,
            "mapping_sha256": mapping.document_sha256,
            "source_sha256": job.source_sha256,
        }
        transition = append(
            job,
            latest,
            state="approved",
            actor_id=actor,
            mapping=mapping,
            report=signed,
            digest=contract.document_digest(signed),
            rollback_window_hours=plan.rollback_window_hours,
        )
        return job_view(job, transition)


def import_job(*, clinic_id: UUID, job_id: UUID) -> ImportJobView:
    """Read one job's current state."""
    with transaction.atomic():
        import_authority(clinic_id=clinic_id)
        return job_view(record(ImportJob, clinic_id=clinic_id, record_id=job_id))


def import_jobs(*, clinic_id: UUID) -> tuple[ImportJobView, ...]:
    """List the clinic's jobs, newest first."""
    with transaction.atomic():
        import_authority(clinic_id=clinic_id)
        return tuple(
            job_view(job)
            for job in ImportJob.objects.filter(clinic_id=clinic_id).order_by(
                "-created_at", "id"
            )
        )


def dry_run_report(*, clinic_id: UUID, job_id: UUID) -> DryRunReport:
    """Return the latest recorded dry run of one job."""
    with transaction.atomic():
        identity_authority(clinic_id=clinic_id)
        job = record(ImportJob, clinic_id=clinic_id, record_id=job_id)
        dry_run = (
            ImportTransition.objects.filter(job_id=job.pk, state="dry_run")
            .order_by("-sequence")
            .first()
        )
        if dry_run is None:
            raise ImportConflictError(Code.INTEROP_TRANSITION)
        return DryRunReport(
            job=job_view(job),
            digest=dry_run.digest,
            report=cast("dict[str, object]", dry_run.report),
        )


EXPORT_HEADER: Final = ("dataset", "row", "source_id", "field", "code")


def export_import_report(*, clinic_id: UUID, job_id: UUID) -> bytes:
    """Export row-level reasons as spreadsheet-safe CSV (no live formulas)."""
    report = dry_run_report(clinic_id=clinic_id, job_id=job_id)
    rows = report.report.get("rejections")
    lines = [
        [str(item[0]), str(item[1]), str(item[4]), str(item[2]), str(item[3])]
        for item in (rows if isinstance(rows, list) else [])
        if isinstance(item, list) and len(item) == len(EXPORT_HEADER)
    ]
    with transaction.atomic():
        identity_authority(clinic_id=clinic_id)
        record_phase1_event(
            "interop.report.exported",
            clinic_id=clinic_id,
            affected_record_id=report.job.job_id,
        )
    return write_csv(EXPORT_HEADER, lines)
