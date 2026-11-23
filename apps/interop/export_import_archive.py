"""Verify a bounded v5 export before handing any of its values to the importer."""

from __future__ import annotations

import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import rfc8785

from apps.interop import sources
from apps.interop.access import Code, ImportConflictError, ImportInputError
from apps.interop.export_registry import FORMAT, datasets

if TYPE_CHECKING:
    from apps.interop.webhooks import JsonValue

ATTACHMENT_PATH: Final = re.compile(
    r"attachments/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.(?:pdf|png|jpg)"
)


@dataclass(frozen=True, slots=True)
class VerifiedExport:
    """JSONL projections and any requested attachment bytes from a verified ZIP."""

    rows: dict[str, list[dict[str, object]]]
    attachments: dict[str, bytes]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError
    return obj


def _invalid_constant(_value: str) -> object:
    raise ValueError


def _object(raw: bytes, code: Code) -> dict[str, object]:
    try:
        obj: object = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (ValueError, RecursionError):
        raise ImportInputError(code) from None
    if not isinstance(obj, dict):
        raise ImportInputError(code)
    return obj


def _manifest(raw: bytes) -> dict[str, object]:
    obj = _object(raw, Code.MANIFEST_MISMATCH)
    if obj.get("v") != FORMAT:
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    digest = obj.pop("manifest_sha256", None)
    try:
        canonical = rfc8785.dumps(cast("JsonValue", obj))
    except (ValueError, RecursionError):
        raise ImportInputError(Code.MANIFEST_MISMATCH) from None
    if digest != sources.sha256_hex(canonical):
        raise ImportConflictError(Code.CHECKSUM_MISMATCH)
    return obj


def _dataset_counts(manifest: dict[str, object]) -> dict[str, int]:
    by_relation = {entry.relation: entry.domain for entry in datasets()}
    entries = manifest.get("datasets")
    if not isinstance(entries, list):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    counts: dict[str, int] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        relation, count = item.get("relation"), item.get("records")
        if (
            not isinstance(relation, str)
            or relation not in by_relation
            or relation in counts
            or item.get("domain") != by_relation[relation]
            or item.get("jsonl") != f"data/{relation}.jsonl"
            or item.get("csv") != f"data/{relation}.csv"
            or type(count) is not int
            or count < 0
        ):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        counts[relation] = count
    if (
        counts.keys() != by_relation.keys()
        or manifest.get("dataset_count") != len(counts)
        or manifest.get("record_count") != sum(counts.values())
    ):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return counts


def _file_facts(
    manifest: dict[str, object], *, paths: dict[str, str]
) -> dict[str, tuple[object, int]]:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    declared: dict[str, tuple[object, int]] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        path, size = item.get("path"), item.get("bytes")
        if (
            not isinstance(path, str)
            or path not in paths
            or path in declared
            or item.get("kind") != paths[path]
            or type(size) is not int
            or size < 0
        ):
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        declared[path] = item.get("sha256"), size
    if declared.keys() != paths.keys():
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return declared


def _rows(raw: bytes, count: int) -> list[dict[str, object]]:
    if count > sources.MAX_ROWS:
        raise ImportInputError(Code.TOO_MANY_ROWS)
    if raw.count(b"\n") != count or (raw and not raw.endswith(b"\n")):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    return [_object(line, Code.MALFORMED_JSON) for line in raw.splitlines()]


def _checked_members(
    archive: zipfile.ZipFile, relations: set[str], *, keep_attachments: bool
) -> VerifiedExport:
    data_paths = {
        f"data/{dataset.relation}.{kind}": kind
        for dataset in datasets()
        for kind in ("jsonl", "csv")
    }
    members = sources.bounded_members(
        archive,
        allowed_file=lambda name: (
            name in data_paths
            or name == sources.MANIFEST
            or ATTACHMENT_PATH.fullmatch(name) is not None
        ),
        required_files={sources.MANIFEST, *data_paths},
    )
    manifest = _manifest(sources.read_member(archive, members.pop(sources.MANIFEST)))
    counts = _dataset_counts(manifest)
    attachment_paths = members.keys() - data_paths.keys()
    if manifest.get("attachment_count") != len(attachment_paths):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    facts = _file_facts(
        manifest, paths=data_paths | dict.fromkeys(attachment_paths, "attachment")
    )
    retained = {f"data/{relation}.jsonl": relation for relation in relations}
    rows: dict[str, list[dict[str, object]]] = {}
    attachments: dict[str, bytes] = {}
    for path, info in members.items():
        digest, size = facts[path]
        if size != info.file_size:
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        raw = sources.read_member(archive, info)
        if sources.sha256_hex(raw) != digest:
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
        if path in retained:
            relation = retained[path]
            rows[relation] = _rows(raw, counts[relation])
        elif keep_attachments and path in attachment_paths:
            attachments[path] = raw
    return VerifiedExport(rows=rows, attachments=attachments)


def verify_export(
    data: bytes, relations: set[str], *, keep_attachments: bool
) -> VerifiedExport:
    """Verify the v5 manifest and every member, retaining only mapped inputs."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _checked_members(
                archive, relations, keep_attachments=keep_attachments
            )
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, zlib.error):
        raise ImportInputError(Code.MALFORMED_ARCHIVE) from None
