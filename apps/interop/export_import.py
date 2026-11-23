"""Project selected v5 datasets into todo 66's governed import, not a restore."""

from __future__ import annotations

import csv
import io
import zipfile
from typing import TYPE_CHECKING, Final

import rfc8785
from django.conf import settings

from apps.ehr.attachments import EXTENSIONS
from apps.interop import contract, sources
from apps.interop.access import (
    Code,
    ImportConflictError,
    ImportInputError,
    import_authority,
)
from apps.interop.export_import_archive import VerifiedExport, verify_export
from apps.interop.jobs import upload_import, verified_upload_digest

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from uuid import UUID

    from apps.interop.jobs import ImportJobView
    from apps.interop.webhooks import JsonValue

PATIENTS: Final = "intake_patient"
DEMOGRAPHICS: Final = "intake_patientdemographics"
IDENTIFIERS: Final = "intake_patientidentifier"
NOTES: Final = "ehr_clinicaldocumentversion"
OBSERVATIONS: Final = "ehr_observation"
ATTACHMENTS: Final = "ehr_clinicalattachment"
IMPORTABLE_DATASETS: Final = frozenset(
    {PATIENTS, DEMOGRAPHICS, IDENTIFIERS, NOTES, OBSERVATIONS, ATTACHMENTS}
)
_ENROLLMENTS: Final = "intake_patientclinicenrollment"
_HIDDEN: Final = "interop_hiddenenrollment"
_CORRECTIONS: Final = "intake_demographicscorrection"
_DOCUMENTS: Final = "ehr_clinicaldocument"
_ENCOUNTERS: Final = "ehr_encounter"
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


def _text(row: dict[str, object], name: str, *, nullable: bool = False) -> str:
    value = row.get(name)
    if nullable and value is None and name in row:
        return ""
    if not isinstance(value, str) or (not nullable and not value):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return value


def _by_id(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    indexed = {_text(row, "id"): row for row in rows}
    if len(indexed) != len(rows):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return indexed


def _current(
    rows: Iterable[dict[str, object]], *key: str
) -> Iterable[dict[str, object]]:
    latest: dict[tuple[str, ...], tuple[int, dict[str, object]]] = {}
    seen: set[tuple[tuple[str, ...], int]] = set()
    for row in rows:
        identity = tuple(_text(row, name) for name in key)
        version = row.get("version")
        if type(version) is not int or version < 1 or (identity, version) in seen:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        seen.add((identity, version))
        if identity not in latest or version > latest[identity][0]:
            latest[identity] = version, row
    return (row for _, row in latest.values())


def _visible_scopes(verified: VerifiedExport) -> set[tuple[str, str]]:
    hidden = {_text(row, "enrollment_id") for row in verified.rows[_HIDDEN]}
    return {
        (_text(row, "patient_id"), _text(row, "clinic_id"))
        for row in verified.rows[_ENROLLMENTS]
        if _text(row, "id") not in hidden
    }


def _patient_rows(
    verified: VerifiedExport, visible_scopes: set[tuple[str, str]]
) -> dict[str, dict[str, str]]:
    patient_ids = {patient_id for patient_id, _ in visible_scopes}
    authors = {
        _text(row, "patient_id"): _text(row, "actor_label")
        for row in verified.rows[_CORRECTIONS]
        if row.get("previous_id") is None
    }
    patients = {
        patient_id: {
            "source_id": patient_id,
            "legal_name": _text(row, "full_name"),
            "birth_date": _text(row, "birth_date", nullable=True),
            "registered_at": _text(row, "created_at"),
            "registered_by": authors.get(patient_id, ""),
        }
        for patient_id, row in _by_id(verified.rows[PATIENTS]).items()
        if patient_id in patient_ids
    }
    if patient_ids != patients.keys():
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    for row in _current(verified.rows.get(DEMOGRAPHICS, []), "patient_id"):
        patient = patients.get(_text(row, "patient_id"))
        if patient is not None:
            patient.update(
                (name, _text(row, name, nullable=True))
                for name in ("legal_name", "social_name", "sex_at_birth")
            )
    identifiers = (
        row
        for row in verified.rows.get(IDENTIFIERS, [])
        if _text(row, "kind") in _SOURCE_IDENTIFIER_KINDS
    )
    for row in _current(identifiers, "patient_id", "kind"):
        patient = patients.get(_text(row, "patient_id"))
        if patient is not None and row.get("retired_at") is None:
            patient[_text(row, "kind")] = _text(row, "value")
    return patients


def _note_rows(
    rows: dict[str, list[dict[str, object]]], visible_scopes: set[tuple[str, str]]
) -> Iterable[dict[str, str]]:
    documents = _by_id(rows.get(_DOCUMENTS, []))
    encounters = _by_id(rows.get(_ENCOUNTERS, []))
    for row in rows.get(NOTES, []):
        if row.get("state") not in ("finalized", "superseded"):
            continue
        document = documents.get(_text(row, "document_id"))
        encounter = (
            encounters.get(_text(document, "encounter_id")) if document else None
        )
        if encounter is None:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        patient_id = _text(encounter, "patient_id")
        if (patient_id, _text(encounter, "clinic_id")) not in visible_scopes:
            continue
        content = row.get("content")
        if not isinstance(content, dict):
            raise ImportInputError(Code.MALFORMED_JSON)
        try:
            canonical = rfc8785.dumps(content)
        except (ValueError, RecursionError):
            raise ImportInputError(Code.MALFORMED_JSON) from None
        if sources.sha256_hex(canonical) != row.get("content_sha256"):
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        yield {
            "source_id": _text(row, "id"),
            "patient_source_id": patient_id,
            "kind": "note",
            "recorded_at": _text(row, "finalized_at"),
            "author": _text(row, "author_id"),
            "text": canonical.decode("utf-8"),
        }


def _observation_rows(
    rows: list[dict[str, object]], visible_scopes: set[tuple[str, str]]
) -> Iterable[dict[str, str]]:
    for row in rows:
        patient_id = _text(row, "patient_id")
        if (patient_id, _text(row, "clinic_id")) not in visible_scopes:
            continue
        value = row.get("value")
        if not isinstance(value, dict) or row.get("code_set_version") != 1:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        yield {
            "source_id": _text(row, "id"),
            "patient_source_id": patient_id,
            "kind": "observation",
            "recorded_at": _text(row, "effective_at"),
            "author": _text(row, "performer_id"),
            "code": _text(row, "kind"),
            "value": _text(value, "value"),
            "unit": _text(row, "unit"),
        }


def _attachment_rows(
    verified: VerifiedExport,
    visible_scopes: set[tuple[str, str]],
    members: dict[str, bytes],
) -> Iterable[dict[str, str]]:
    for row in verified.rows.get(ATTACHMENTS, []):
        patient_id = _text(row, "patient_id")
        if (
            row.get("state") != "available"
            or (patient_id, _text(row, "clinic_id")) not in visible_scopes
        ):
            continue
        source_id = _text(row, "id")
        extension = EXTENSIONS.get(_text(row, "detected_type"))
        filename = f"{source_id}.{extension}"
        path = f"files/{filename}"
        raw = verified.attachments.get(f"attachments/{filename}")
        if raw is None or path in members:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        if len(raw) != row.get("size_bytes") or sources.sha256_hex(raw) != row.get(
            "sha256"
        ):
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        members[path] = raw
        yield {
            "source_id": source_id,
            "patient_source_id": patient_id,
            "kind": "attachment",
            "recorded_at": _text(row, "created_at"),
            "author": _text(row, "uploader_id"),
            "title": _text(row, "file_name"),
            "file": filename,
        }


def _table(fields: Sequence[str], rows: Iterable[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    # Keep source values verbatim; this private staging file is not a report
    # for spreadsheet use. Formula-like patient names still fail the dry run.
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    writer.writerows(rows)
    try:
        return stream.getvalue().encode("utf-8")
    except UnicodeEncodeError:
        raise ImportInputError(Code.INVALID_ENCODING) from None


def _project(verified: VerifiedExport) -> bytes:
    visible_scopes = _visible_scopes(verified)
    patients = _patient_rows(verified, visible_scopes)
    members: dict[str, bytes] = {}
    history = (
        *_note_rows(verified.rows, visible_scopes),
        *_observation_rows(verified.rows.get(OBSERVATIONS, []), visible_scopes),
        *_attachment_rows(verified, visible_scopes, members),
    )
    members.update(
        {
            sources.DATASET_FILES["patients"]: _table(
                contract.PATIENT_FIELDS, patients.values()
            ),
            sources.DATASET_FILES["history"]: _table(contract.HISTORY_FIELDS, history),
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
    import_authority(clinic_id=clinic_id)
    verified_upload_digest(data, declared_sha256)
    lookups = {_ENROLLMENTS, _HIDDEN, _CORRECTIONS}
    if NOTES in selected:
        lookups.update((_DOCUMENTS, _ENCOUNTERS))
    verified = verify_export(
        data, selected | lookups, keep_attachments=ATTACHMENTS in selected
    )
    projected = _project(verified)
    return upload_import(
        clinic_id=clinic_id,
        source_kind="zip",
        data=projected,
        declared_sha256=sources.sha256_hex(projected),
        idempotency_key=idempotency_key,
    )
