"""Project visible clinical history from native and previously imported facts."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Final

import rfc8785

from apps.ehr.attachments import EXTENSIONS
from apps.ehr.models import ClinicalAttachment, ClinicalDocumentVersion
from apps.interop import sources
from apps.interop.access import Code, ImportConflictError, ImportInputError
from apps.interop.export_import_archive import by_id, text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apps.interop.export_import_archive import VerifiedExport

NOTES: Final = "ehr_clinicaldocumentversion"
OBSERVATIONS: Final = "ehr_observation"
ATTACHMENTS: Final = "ehr_clinicalattachment"
DOCUMENTS: Final = "ehr_clinicaldocument"
ENCOUNTERS: Final = "ehr_encounter"
IMPORTED_RECORDS: Final = "interop_importedrecord"
IMPORTED_FILES: Final = "interop_importfile"
IMPORT_TRANSITIONS: Final = "interop_importtransition"


def committed_records(
    rows: dict[str, list[dict[str, object]]],
    visible_enrollments: set[str],
    *,
    hidden_enrollments: set[str],
) -> list[dict[str, object]]:
    """Use only committed jobs that have not rolled back, and visible enrollments."""
    states: dict[str, set[str]] = {}
    for row in rows[IMPORT_TRANSITIONS]:
        states.setdefault(text(row, "job_id"), set()).add(text(row, "state"))
    eligible: list[dict[str, object]] = []
    for record in by_id(rows[IMPORTED_RECORDS]).values():
        job = states.get(text(record, "job_id"))
        if job is None:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        if "committed" not in job or "rolled_back" in job:
            continue
        enrollment = text(record, "enrollment_id")
        if enrollment in hidden_enrollments:
            continue
        if enrollment not in visible_enrollments:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        eligible.append(record)
    return eligible


def _attachment(
    record: dict[str, object],
    files: dict[str, dict[str, object]],
    content: dict[str, object],
    members: dict[str, bytes],
) -> str:
    stored = files.get(text(record, "file_id"))
    if (
        stored is None
        or stored.get("role") != "attachment"
        or stored.get("scan_verdict") != "clean"
        or stored.get("job_id") != record.get("job_id")
    ):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    try:
        raw = base64.b64decode(text(stored, "data"), validate=True)
    except ValueError:
        raise ImportInputError(Code.MALFORMED_JSON) from None
    if (
        len(raw) != stored.get("size_bytes")
        or sources.sha256_hex(raw) != stored.get("sha256")
        or any(
            content.get(k) != stored.get(k)
            for k in ("media_type", "sha256", "size_bytes")
        )
        or "title" not in content
    ):
        raise ImportConflictError(Code.CHECKSUM_MISMATCH)
    suffix = text(stored, "path").rpartition(".")[2].lower()
    filename = f"prior-{text(stored, 'id')}.{suffix}"
    members[f"files/{filename}"] = raw
    return filename


def history_rows(
    rows: list[dict[str, object]],
    files: dict[str, dict[str, object]],
    members: dict[str, bytes],
) -> Iterable[dict[str, str]]:
    """Retain source labels and times, and stage only linked attachment bytes."""
    for row in rows:
        kind = text(row, "kind")
        if kind == "patient" and row.get("dataset") == "patients":
            continue
        content = row.get("content")
        if row.get("dataset") != "history" or not isinstance(content, dict):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        fields = {
            "source_id": text(row, "id"),
            "original_source_id": text(row, "source_record_id"),
            "patient_source_id": text(row, "patient_id"),
            "kind": kind,
            "author": text(row, "original_author"),
            "recorded_at": text(row, "original_recorded_at"),
        }
        if kind == "note":
            fields["text"] = text(content, "text")
        elif kind == "observation" and content.get("code_set_version") == 1:
            fields.update(
                (key, text(content, key)) for key in ("code", "value", "unit")
            )
        elif kind == "attachment":
            fields["file"] = _attachment(row, files, content, members)
            fields["title"] = text(content, "title", nullable=True)
            fields.update(
                (key, str(content[key]))
                for key in ("media_type", "sha256", "size_bytes")
            )
        else:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        yield fields


def note_rows(
    rows: dict[str, list[dict[str, object]]], visible_scopes: set[tuple[str, str]]
) -> Iterable[dict[str, str]]:
    """Preserve finalized SOAP content and its original lineage."""
    documents = by_id(rows.get(DOCUMENTS, []))
    encounters = by_id(rows.get(ENCOUNTERS, []))
    for row in rows.get(NOTES, []):
        state = text(row, "state")
        if state not in ClinicalDocumentVersion.State.values:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        if state in (
            ClinicalDocumentVersion.State.DRAFT,
            ClinicalDocumentVersion.State.DISCARDED,
        ):
            continue
        document = documents.get(text(row, "document_id"))
        encounter = encounters.get(text(document, "encounter_id")) if document else None
        if encounter is None:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        patient_id = text(encounter, "patient_id")
        if (patient_id, text(encounter, "clinic_id")) not in visible_scopes:
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
            "source_id": text(row, "id"),
            "patient_source_id": patient_id,
            "kind": "note",
            "recorded_at": text(row, "finalized_at"),
            "author": text(row, "author_id"),
            "text": canonical.decode("utf-8"),
        }


def observation_rows(
    rows: list[dict[str, object]], visible_scopes: set[tuple[str, str]]
) -> Iterable[dict[str, str]]:
    """Project visible observations with their source code, unit and precision."""
    for row in rows:
        patient_id = text(row, "patient_id")
        if (patient_id, text(row, "clinic_id")) not in visible_scopes:
            continue
        value = row.get("value")
        if not isinstance(value, dict) or row.get("code_set_version") != 1:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        yield {
            "source_id": text(row, "id"),
            "patient_source_id": patient_id,
            "kind": "observation",
            "recorded_at": text(row, "effective_at"),
            "author": text(row, "performer_id"),
            "code": text(row, "kind"),
            "value": text(value, "value"),
            "unit": text(row, "unit"),
        }


def attachment_rows(
    verified: VerifiedExport,
    visible_scopes: set[tuple[str, str]],
    members: dict[str, bytes],
) -> Iterable[dict[str, str]]:
    """Stage available documents only after their bytes match the export facts."""
    for row in verified.rows.get(ATTACHMENTS, []):
        patient_id = text(row, "patient_id")
        state = text(row, "state")
        if state not in ClinicalAttachment.State.values:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        if (
            state != ClinicalAttachment.State.AVAILABLE
            or (patient_id, text(row, "clinic_id")) not in visible_scopes
        ):
            continue
        source_id = text(row, "id")
        extension = EXTENSIONS.get(text(row, "detected_type"))
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
            "recorded_at": text(row, "created_at"),
            "author": text(row, "uploader_id"),
            "title": text(row, "file_name"),
            "file": filename,
            "media_type": text(row, "detected_type"),
            "size_bytes": str(len(raw)),
            "sha256": sources.sha256_hex(raw),
        }
