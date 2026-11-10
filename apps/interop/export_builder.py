"""Build one organization export archive under the requester's current authority.

Every page is read and decrypted in its own ``tenant_context`` of the requesting
administrator, so revoking that authority stops the build at the next page. The
archive is a deterministic zip (fixed timestamps, sorted datasets) holding
``data/<relation>.jsonl`` and ``.csv`` per dataset, decrypted clinical
attachments under ``attachments/`` and a ``manifest.json`` whose file digests
and ``manifest_sha256`` the retention export verifier already checks. The
archive is stored as encrypted byte-range parts until it expires.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final
from uuid import UUID

import rfc8785
from django.conf import settings
from django.db import DatabaseError, connection
from django.db.models import F
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.attachment_storage import AttachmentStorageError, default_storage
from apps.ehr.attachments import EXTENSIONS
from apps.interop.export_registry import (
    EXCLUDED,
    EXCLUSION_REASONS,
    FORMAT,
    Column,
    Dataset,
    datasets,
)
from apps.interop.models import OrganizationExport, OrganizationExportPart
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.envelope import EnvelopeError, reveal

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.interop.webhooks import JsonValue

PAGE_SIZE: Final = 500
DEFAULT_PART_BYTES: Final = 4 * 1024 * 1024
MAX_PART_BYTES: Final = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024 * 1024
MANIFEST_NAME: Final = "manifest.json"
ZIP_DATE: Final = (1980, 1, 1, 0, 0, 0)
ATTACHMENT_RELATION: Final = "ehr_clinicalattachment"
_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")
_TEXT_TYPES: Final = frozenset({"text"})
AUTHORITY_REVOKED: Final = "authority_revoked"
DECRYPT_FAILED: Final = "decrypt_failed"
ATTACHMENT_UNAVAILABLE: Final = "attachment_unavailable"
ATTACHMENT_MISMATCH: Final = "attachment_mismatch"
TOO_LARGE: Final = "too_large"


class ExportFailureError(Exception):
    """A closed failure reason; no record content is carried."""

    def __init__(self, reason: str) -> None:
        """Keep the machine reason stored on the export receipt."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Scope:
    export_id: UUID
    organization_id: UUID
    requested_by_id: UUID
    clinic_id: UUID


@dataclass
class _Facts:
    files: list[dict[str, JsonValue]] = field(default_factory=list)
    datasets: list[dict[str, JsonValue]] = field(default_factory=list)
    attachments: list[tuple[UUID, str, str, str]] = field(default_factory=list)
    records: int = 0


def _part_bytes() -> int:
    configured = getattr(settings, "INTEROP_EXPORT_PART_BYTES", DEFAULT_PART_BYTES)
    if type(configured) is not int or not 1 <= configured <= MAX_PART_BYTES:
        return DEFAULT_PART_BYTES
    return configured


def neutralize(value: str) -> str:
    """Prefix spreadsheet formula triggers so a CSV cell is never executed."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _decode_envelope(column: Column, value: object) -> object:
    if not isinstance(value, str) or not value.startswith("\\x"):
        return value
    raw = bytes.fromhex(value[2:])
    if column.purpose is None:
        return base64.b64encode(raw).decode("ascii")
    plain = reveal(purpose=column.purpose, envelope=raw)
    if column.type == "json":
        return json.loads(plain.decode("utf-8"))
    if column.type == "base64":
        return base64.b64encode(plain).decode("ascii")
    return plain.decode("utf-8")


def _readable(dataset: Dataset, row: dict[str, object]) -> dict[str, object]:
    """Decrypt envelopes and encode other bytes, inside the tenant transaction."""
    columns = {column.name: column for column in dataset.columns}
    result: dict[str, object] = {}
    for name, value in row.items():
        column = columns.get(name)
        if column is not None and (
            column.purpose is not None or column.type == "base64"
        ):
            result[name] = _decode_envelope(column, value)
        else:
            result[name] = value
    return result


def _page(
    scope: _Scope, dataset: Dataset, after: str | None
) -> list[tuple[str, dict[str, object]]]:
    try:
        with tenant_context(scope.requested_by_id, scope.organization_id):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT row_key,row_data FROM clinic_app.interop_export_rows"
                    "(%s,%s,%s,%s)",
                    [scope.export_id, dataset.relation, after, PAGE_SIZE],
                )
                rows = cursor.fetchall()
            return [
                (
                    str(key),
                    _readable(
                        dataset,
                        json.loads(data) if isinstance(data, str) else dict(data),
                    ),
                )
                for key, data in rows
            ]
    except TenantAccessDeniedError as error:
        raise ExportFailureError(AUTHORITY_REVOKED) from error
    except EnvelopeError as error:
        raise ExportFailureError(DECRYPT_FAILED) from error
    except DatabaseError as error:
        if getattr(error.__cause__, "sqlstate", None) == "42501":
            raise ExportFailureError(AUTHORITY_REVOKED) from error
        raise


def _rows(scope: _Scope, dataset: Dataset) -> Iterator[dict[str, object]]:
    after: str | None = None
    while True:
        page = _page(scope, dataset, after)
        for _key, row in page:
            yield row
        if len(page) < PAGE_SIZE:
            return
        after = page[-1][0]


def _cell(column_type: str, value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    text = str(value)
    return neutralize(text) if column_type in _TEXT_TYPES else text


def _add(
    archive: zipfile.ZipFile, facts: _Facts, name: str, source: BinaryIO, kind: str
) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "w", force_zip64=True) as target:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            target.write(chunk)
    facts.files.append(
        {"path": name, "sha256": digest.hexdigest(), "bytes": size, "kind": kind}
    )


def _dataset(
    archive: zipfile.ZipFile, scope: _Scope, dataset: Dataset, work: Path, facts: _Facts
) -> None:
    lines_path = work / "dataset.jsonl"
    table_path = work / "dataset.csv"
    count = 0
    with (
        lines_path.open("w", encoding="utf-8", newline="") as lines,
        table_path.open("w", encoding="utf-8", newline="") as table,
    ):
        writer = csv.writer(table, lineterminator="\r\n")
        header: list[str] | None = None
        types = {column.name: column.type for column in dataset.columns}
        for row in _rows(scope, dataset):
            if header is None:
                # Model columns plus any column the live relation also carries.
                header = sorted({column.name for column in dataset.columns} | set(row))
                writer.writerow(header)
            lines.write(
                json.dumps(
                    row, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                + "\n"
            )
            writer.writerow(
                [_cell(types.get(name, "text"), row.get(name)) for name in header]
            )
            if (
                dataset.relation == ATTACHMENT_RELATION
                and row.get("state") == "available"
            ):
                facts.attachments.append(
                    (
                        UUID(str(row["id"])),
                        str(row["storage_key"]),
                        str(row["sha256"]),
                        str(row["detected_type"]),
                    )
                )
            count += 1
        if header is None:
            writer.writerow(sorted(column.name for column in dataset.columns))
    base = f"data/{dataset.relation}"
    with lines_path.open("rb") as source:
        _add(archive, facts, f"{base}.jsonl", source, "jsonl")
    with table_path.open("rb") as source:
        _add(archive, facts, f"{base}.csv", source, "csv")
    facts.records += count
    facts.datasets.append(
        {
            "relation": dataset.relation,
            "domain": dataset.domain,
            "records": count,
            "jsonl": f"{base}.jsonl",
            "csv": f"{base}.csv",
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "encrypted_at_rest": column.purpose is not None,
                }
                for column in dataset.columns
            ],
        }
    )


def _attachments(archive: zipfile.ZipFile, scope: _Scope, facts: _Facts) -> None:
    storage = default_storage()
    for attachment_id, key, expected, detected in sorted(facts.attachments):
        try:
            envelope = storage.get(key)
            with tenant_context(scope.requested_by_id, scope.organization_id):
                data = reveal(purpose="ehr.clinicalattachment.bytes", envelope=envelope)
        except AttachmentStorageError as error:
            raise ExportFailureError(ATTACHMENT_UNAVAILABLE) from error
        except TenantAccessDeniedError as error:
            raise ExportFailureError(AUTHORITY_REVOKED) from error
        except EnvelopeError as error:
            raise ExportFailureError(DECRYPT_FAILED) from error
        if hashlib.sha256(data).hexdigest() != expected or detected not in EXTENSIONS:
            raise ExportFailureError(ATTACHMENT_MISMATCH)
        name = f"attachments/{attachment_id}.{EXTENSIONS[detected]}"
        _add(archive, facts, name, io.BytesIO(data), "attachment")


def _manifest(scope: _Scope, facts: _Facts) -> dict[str, JsonValue]:
    manifest: dict[str, JsonValue] = {
        "v": FORMAT,
        "export_id": str(scope.export_id),
        "organization_id": str(scope.organization_id),
        "created_at": timezone.now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "dataset_count": len(facts.datasets),
        "record_count": facts.records,
        "attachment_count": len(facts.attachments),
        "datasets": facts.datasets,
        "files": facts.files,
        "excluded": [
            {"relation": relation, "reason": reason, "why": EXCLUSION_REASONS[reason]}
            for relation, reason in sorted(EXCLUDED.items())
        ],
    }
    manifest["manifest_sha256"] = hashlib.sha256(rfc8785.dumps(manifest)).hexdigest()
    return manifest


def write_archive(scope: _Scope, path: Path) -> tuple[_Facts, str]:
    """Write the complete archive to ``path``; return its facts and manifest digest."""
    facts = _Facts()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for dataset in datasets():
            _dataset(archive, scope, dataset, path.parent, facts)
        _attachments(archive, scope, facts)
        manifest = _manifest(scope, facts)
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=ZIP_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, rfc8785.dumps(manifest))
    return facts, str(manifest["manifest_sha256"])


def _claim(
    export_id: UUID, organization_id: UUID, requested_by_id: UUID
) -> _Scope | None:
    with tenant_context(requested_by_id, organization_id):
        claimed = OrganizationExport.objects.filter(
            pk=export_id,
            organization_id=organization_id,
            requested_by_id=requested_by_id,
            state="requested",
        ).update(state="building", revision=F("revision") + 1)
        if claimed != 1:
            return None
        clinic_id = (
            OrganizationExport.objects.filter(pk=export_id)
            .values_list("clinic_id", flat=True)
            .get()
        )
    return _Scope(export_id, organization_id, requested_by_id, clinic_id)


def _store(scope: _Scope, path: Path, facts: _Facts, manifest_sha256: str) -> None:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise ExportFailureError(TOO_LARGE)
    archive_digest = hashlib.sha256()
    part = 0
    with path.open("rb") as source:
        while chunk := source.read(_part_bytes()):
            part += 1
            archive_digest.update(chunk)
            with tenant_context(scope.requested_by_id, scope.organization_id):
                OrganizationExportPart.objects.create(
                    organization_id=scope.organization_id,
                    export_id=scope.export_id,
                    part=part,
                    data=chunk,
                    sha256=hashlib.sha256(chunk).hexdigest(),
                    size_bytes=len(chunk),
                    created_at=timezone.now(),
                )
    with tenant_context(scope.requested_by_id, scope.organization_id):
        OrganizationExport.objects.filter(pk=scope.export_id, state="building").update(
            state="ready",
            revision=F("revision") + 1,
            part_count=part,
            archive_bytes=size,
            archive_sha256=archive_digest.hexdigest(),
            manifest_sha256=manifest_sha256,
            dataset_count=len(facts.datasets),
            record_count=facts.records,
            attachment_count=len(facts.attachments),
        )
        record_phase1_event(
            "interop.organization_export.ready",
            clinic_id=scope.clinic_id,
            affected_record_id=scope.export_id,
        )


def _fail(scope: _Scope, reason: str) -> None:
    """Record the failure when authority remains; otherwise the sweep fails it."""
    try:
        with tenant_context(scope.requested_by_id, scope.organization_id):
            failed = OrganizationExport.objects.filter(
                pk=scope.export_id, state="building"
            ).update(state="failed", revision=F("revision") + 1, failure_reason=reason)
            if failed == 1:
                record_phase1_event(
                    "interop.organization_export.failed",
                    clinic_id=scope.clinic_id,
                    affected_record_id=scope.export_id,
                )
    except TenantAccessDeniedError:
        # interop_expire_exports fails a building export whose requester lost
        # organization authority; nothing here can write without that authority.
        return


def build_organization_export(
    *, export_id: UUID, organization_id: UUID, requested_by_id: UUID
) -> str:
    """Claim, build and store one requested export; return its resulting state."""
    try:
        scope = _claim(export_id, organization_id, requested_by_id)
    except TenantAccessDeniedError:
        return "requested"
    if scope is None:
        return "skipped"
    with tempfile.TemporaryDirectory(prefix="interop-export-") as work:
        try:
            path = Path(work) / "archive.zip"
            facts, manifest_sha256 = write_archive(scope, path)
            _store(scope, path, facts, manifest_sha256)
        except ExportFailureError as failure:
            _fail(scope, failure.reason)
            return "failed"
    return "ready"
