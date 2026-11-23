"""Project selected v5 datasets into todo 66's governed import, not a restore."""

from __future__ import annotations

import csv
import io
import zipfile
from typing import TYPE_CHECKING, Final

import rfc8785
from django.conf import settings

from apps.interop import contract, sources
from apps.interop.access import (
    Code,
    ImportConflictError,
    ImportInputError,
    import_authority,
)
from apps.interop.export_import_archive import (
    VerifiedExport,
    by_id,
    text,
    verify_export,
)
from apps.interop.export_import_history import (
    ATTACHMENTS,
    DOCUMENTS,
    ENCOUNTERS,
    IMPORT_TRANSITIONS,
    IMPORTED_FILES,
    IMPORTED_RECORDS,
    NOTES,
    OBSERVATIONS,
    attachment_rows,
    committed_records,
    history_rows,
    note_rows,
    observation_rows,
)
from apps.interop.jobs import V5_PROJECTION, stage_upload, verified_upload_digest
from apps.interop.projection import source_values, summarize
from apps.interop.validation import canonical_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from uuid import UUID

    from apps.interop.jobs import ImportJobView
    from apps.interop.webhooks import JsonValue

PATIENTS: Final = "intake_patient"
DEMOGRAPHICS: Final = "intake_patientdemographics"
IDENTIFIERS: Final = "intake_patientidentifier"
IMPORTABLE_DATASETS: Final = frozenset(
    {
        PATIENTS,
        DEMOGRAPHICS,
        IDENTIFIERS,
        NOTES,
        OBSERVATIONS,
        ATTACHMENTS,
        IMPORTED_RECORDS,
        IMPORTED_FILES,
    }
)
_ENROLLMENTS: Final = "intake_patientclinicenrollment"
_HIDDEN: Final = "interop_hiddenenrollment"
_CORRECTIONS: Final = "intake_demographicscorrection"
_SOURCE_IDENTIFIER_KINDS: Final = {"cpf", "cns"}


def v5_import_mapping() -> dict[str, object]:
    """Return the mapping the operator saves and applies to a projected archive."""
    return {
        "schema": contract.SCHEMA_NAME,
        "source_kind": "zip",
        "date_format": "YYYY-MM-DD",
        "datetime_format": contract.OFFSET_DATETIME,
        "datasets": {
            "patients": {"columns": {name: name for name in contract.PATIENT_FIELDS}},
            "history": {"columns": {name: name for name in contract.HISTORY_FIELDS}},
        },
    }


def _current(
    rows: Iterable[dict[str, object]], *key: str
) -> Iterable[dict[str, object]]:
    latest: dict[tuple[str, ...], tuple[int, dict[str, object]]] = {}
    seen: set[tuple[tuple[str, ...], int]] = set()
    for row in rows:
        identity = tuple(text(row, name) for name in key)
        version = row.get("version")
        if type(version) is not int or version < 1 or (identity, version) in seen:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        seen.add((identity, version))
        if identity not in latest or version > latest[identity][0]:
            latest[identity] = version, row
    return (row for _, row in latest.values())


def _visible_scopes(verified: VerifiedExport) -> set[tuple[str, str]]:
    enrollments = by_id(verified.rows[_ENROLLMENTS])
    hidden = {text(row, "enrollment_id") for row in verified.rows[_HIDDEN]}
    if not hidden <= enrollments.keys() or len(hidden) != len(verified.rows[_HIDDEN]):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return {
        (text(row, "patient_id"), text(row, "clinic_id"))
        for enrollment_id, row in enrollments.items()
        if enrollment_id not in hidden
    }


def _patient_rows(
    verified: VerifiedExport, visible_scopes: set[tuple[str, str]]
) -> dict[str, dict[str, str]]:
    patient_ids = {patient_id for patient_id, _ in visible_scopes}
    authors: dict[str, str] = {}
    for row in verified.rows[_CORRECTIONS]:
        previous_id = text(row, "previous_id", nullable=True)
        if previous_id:
            continue
        patient_id = text(row, "patient_id")
        if patient_id in authors:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        authors[patient_id] = text(row, "actor_label")
    patients = {
        patient_id: {
            "source_id": patient_id,
            "legal_name": text(row, "full_name"),
            "birth_date": text(row, "birth_date", nullable=True),
            "registered_at": text(row, "created_at"),
            "registered_by": authors.get(patient_id, ""),
        }
        for patient_id, row in by_id(verified.rows[PATIENTS]).items()
        if patient_id in patient_ids
    }
    if patient_ids != patients.keys():
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    for row in _current(verified.rows.get(DEMOGRAPHICS, []), "patient_id"):
        patient = patients.get(text(row, "patient_id"))
        if patient is not None:
            patient.update(
                (name, text(row, name, nullable=True))
                for name in ("legal_name", "social_name", "sex_at_birth")
            )
    identifiers = (
        row
        for row in verified.rows.get(IDENTIFIERS, [])
        if text(row, "kind") in _SOURCE_IDENTIFIER_KINDS
    )
    for row in _current(identifiers, "patient_id", "kind"):
        patient = patients.get(text(row, "patient_id"))
        retired_at = text(row, "retired_at", nullable=True)
        if patient is not None and not retired_at:
            patient[text(row, "kind")] = text(row, "value")
    return patients


def _table(fields: Sequence[str], rows: Iterable[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    # Keep source values verbatim; this private staging file is not a report
    # for spreadsheet use. Formula-like patient names still fail the dry run.
    writer = csv.DictWriter(stream, fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    try:
        return stream.getvalue().encode("utf-8")
    except UnicodeEncodeError:
        raise ImportInputError(Code.INVALID_ENCODING) from None


def _project(verified: VerifiedExport, selected: set[str]) -> bytes:
    visible_scopes = _visible_scopes(verified)
    patients = _patient_rows(verified, visible_scopes)
    hidden = {text(row, "enrollment_id") for row in verified.rows[_HIDDEN]}
    enrollments = by_id(verified.rows[_ENROLLMENTS])
    prior = committed_records(
        verified.rows, set(enrollments) - hidden, hidden_enrollments=hidden
    )
    if prior and not {IMPORTED_RECORDS, IMPORTED_FILES} <= selected:
        raise ImportInputError(Code.UNSUPPORTED_DATASET)
    origins: set[str] = set()
    for row in prior:
        patient_id = text(row, "patient_id")
        enrollment = enrollments[text(row, "enrollment_id")]
        if enrollment.get("patient_id") != patient_id or enrollment.get(
            "clinic_id"
        ) != row.get("clinic_id"):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        if row.get("kind") == "patient":
            if patient_id in origins:
                raise ImportInputError(Code.MANIFEST_MISMATCH)
            origins.add(patient_id)
            patients[patient_id].update(
                original_source_id=text(row, "source_record_id"),
                registered_by=text(row, "original_author", nullable=True),
                registered_at=text(row, "original_recorded_at", nullable=True),
            )
    members: dict[str, bytes] = {}
    history = (
        *note_rows(verified.rows, visible_scopes),
        *observation_rows(verified.rows.get(OBSERVATIONS, []), visible_scopes),
        *attachment_rows(verified, visible_scopes, members),
        *history_rows(prior, by_id(verified.rows.get(IMPORTED_FILES, [])), members),
    )
    values = list(patients.values())
    if len(origins) + sum("original_source_id" in row for row in history) != len(prior):
        raise ImportConflictError(Code.CHECKSUM_MISMATCH)
    members.update(
        {
            sources.DATASET_FILES["patients"]: _table(contract.PATIENT_FIELDS, values),
            sources.DATASET_FILES["history"]: _table(contract.HISTORY_FIELDS, history),
            sources.PROJECTION_WITNESS: canonical_bytes(
                summarize(source_values(values, history))
            ),
        }
    )
    manifest: dict[str, JsonValue] = {
        "v": sources.ARCHIVE_VERSION,
        "files": [
            {"path": path, "bytes": len(raw), "sha256": sources.sha256_hex(raw)}
            for path, raw in sorted(members.items())
        ],
    }
    output = io.BytesIO()
    # No clock-dependent ZIP metadata: exact retries produce the same checksum.
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(zipfile.ZipInfo(sources.MANIFEST), rfc8785.dumps(manifest))
        for path, raw in sorted(members.items()):
            archive.writestr(zipfile.ZipInfo(path), raw)
    return output.getvalue()


def upload_v5_import(
    *,
    clinic_id: UUID,
    data: bytes,
    declared_sha256: str,
    relations: Sequence[str],
    idempotency_key: UUID,
) -> ImportJobView:
    """Verify a v5 archive and stage only the requested supported projections."""
    if settings.CLINIC_DATA_MODE != "synthetic":
        raise ImportInputError(Code.UNSUPPORTED_SOURCE)
    selected = set(relations)
    if not selected <= IMPORTABLE_DATASETS:
        raise ImportInputError(Code.UNSUPPORTED_DATASET)
    if PATIENTS not in selected or len(selected) != len(relations):
        raise ImportInputError(Code.INVALID_COMMAND)
    if (IMPORTED_RECORDS in selected) != (IMPORTED_FILES in selected):
        raise ImportInputError(Code.INVALID_COMMAND)
    import_authority(clinic_id=clinic_id)
    verified_upload_digest(data, declared_sha256)
    # Prior import facts are always read so that omitting them is refused;
    # native clinical rows and their bytes are read only when selected.
    lookups = {
        _ENROLLMENTS,
        _HIDDEN,
        _CORRECTIONS,
        IMPORTED_RECORDS,
        IMPORT_TRANSITIONS,
    }
    if NOTES in selected:
        lookups.update((DOCUMENTS, ENCOUNTERS))
    verified = verify_export(
        data, selected | lookups, keep_attachments=ATTACHMENTS in selected
    )
    projected = _project(verified, selected)
    return stage_upload(
        clinic_id=clinic_id,
        source_kind=V5_PROJECTION,
        data=projected,
        declared_sha256=sources.sha256_hex(projected),
        idempotency_key=idempotency_key,
    )
