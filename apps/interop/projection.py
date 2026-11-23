"""Count and value commitments for a verified v5 staging archive."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apps.interop.access import Code, ImportInputError
from apps.interop.validation import IDENTIFIER_FIELDS, rows_digest

if TYPE_CHECKING:
    from apps.interop.validation import Normalized


def instant(value: str) -> str | None:
    """Canonicalize the representation of an instant, not the value itself."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        return stamp.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        raise ImportInputError(Code.MANIFEST_MISMATCH) from None


def _original(row: dict[str, str]) -> dict[str, object]:
    if original := row.get("original_source_id"):
        return {"original_source_id": original}
    return {}


def _patient_value(row: dict[str, str], index: int) -> dict[str, object]:
    return {
        "row": index,
        "source_id": row["source_id"],
        **_original(row),
        **{
            key: row.get(key) or None
            for key in (
                "legal_name",
                "social_name",
                "birth_date",
                "sex_at_birth",
                "registered_by",
            )
        },
        "registered_at": instant(row.get("registered_at", "")),
        "identifiers": [
            [kind, row[kind]] for kind in IDENTIFIER_FIELDS if row.get(kind)
        ],
    }


def _clinical_value(
    row: dict[str, str], *, index: int, patient_row: int
) -> dict[str, object]:
    kind = row["kind"]
    content: dict[str, object]
    if kind == "note":
        content = {"text": row["text"]}
    elif kind == "observation":
        content = {k: row[k] for k in ("code", "value", "unit")} | {
            "code_set_version": 1
        }
    else:
        content = {
            "title": row.get("title") or None,
            "media_type": row["media_type"],
            "sha256": row["sha256"],
            "size_bytes": int(row["size_bytes"]),
        }
    return {
        "row": index,
        "patient_row": patient_row,
        "source_id": row["source_id"],
        **_original(row),
        "kind": kind,
        "recorded_at": instant(row["recorded_at"]),
        "author": row["author"],
        "content": content,
        "file": f"files/{row['file']}" if row.get("file") else None,
    }


def source_values(
    patients: list[dict[str, str]], history: tuple[dict[str, str], ...]
) -> dict[str, list[dict[str, object]]]:
    """Commit to v5 values before CSV parsing or the governed mapping."""
    positions = {row["source_id"]: i for i, row in enumerate(patients, 1)}
    if len(positions) != len(patients) or any(
        row["patient_source_id"] not in positions for row in history
    ):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return {
        "patients": [_patient_value(row, i) for i, row in enumerate(patients, 1)],
        "history": [
            _clinical_value(
                row, index=i, patient_row=positions[row["patient_source_id"]]
            )
            for i, row in enumerate(history, 1)
        ],
    }


def summarize(values: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """Return per-dataset cardinality and canonical digest, without values."""
    return {
        name: {"count": len(rows), "sha256": rows_digest(rows)}
        for name, rows in values.items()
    }


def read_witness(raw: bytes) -> dict[str, object]:
    """Decode exactly the two sealed projection witnesses."""
    try:
        data: object = json.loads(raw)
    except (ValueError, RecursionError):
        raise ImportInputError(Code.MANIFEST_MISMATCH) from None
    if not isinstance(data, dict) or data.keys() != {"patients", "history"}:
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    for row in data.values():
        if (
            not isinstance(row, dict)
            or row.keys() != {"count", "sha256"}
            or type(row["count"]) is not int
            or row["count"] < 0
            or not isinstance(row["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
        ):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
    return dict(data)


def mapped_values(normalized: Normalized) -> dict[str, list[dict[str, object]]]:
    """Canonicalize parsed rows independently of the source witness."""
    result = {
        "patients": [row.canonical() for row in normalized.patients],
        "history": [row.canonical() for row in normalized.history],
    }
    for name, field in (("patients", "registered_at"), ("history", "recorded_at")):
        for row in result[name]:
            if isinstance(stamp := row[field], str):
                row[field] = instant(stamp)
    return result
