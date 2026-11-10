"""Bounded, deterministic readers for CSV, XLSX, API JSON and import archives.

Every reader treats the upload as hostile: sizes, row and column counts, cell
lengths, archive layout and compression ratios are bounded before decoding,
XML parts carrying a DTD are refused (no entity expansion is possible) and an
archive may only hold the flat allowlisted layout (``manifest.json``,
``patients.csv``, ``history.csv``, ``files/<name>``), so an entry can never
escape an extraction directory. Nothing is ever extracted to disk.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from xml.etree import ElementTree as ET

from django.conf import settings

from apps.ehr.attachment_scanner import ACTIVE_MARKERS
from apps.interop.access import Code, ImportConflictError, ImportInputError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

MAX_SOURCE_BYTES: Final = 20 * 1024 * 1024
MAX_ROWS: Final = 50_000
MAX_COLUMNS: Final = 64
MAX_HEADER_CHARS: Final = 128
MAX_CELL_CHARS: Final = 20_000
MAX_MEMBERS: Final = 2_000
MAX_MEMBER_BYTES: Final = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES: Final = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 100
SMALL_MEMBER_BYTES: Final = 64 * 1024
ARCHIVE_VERSION: Final = "clinic-import-archive-v1"
MANIFEST: Final = "manifest.json"
DATASET_FILES: Final = {"patients": "patients.csv", "history": "history.csv"}
ATTACHMENT_NAME: Final = re.compile(r"files/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
DELIMITERS: Final = (",", ";", "\t", "|")
ENCODINGS: Final = ("utf-8", "cp1252")
MEDIA_TYPES: Final = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".txt": ("text/plain", ()),
}
SYMLINK_MODE: Final = 0o120000
FILE_TYPE_MASK: Final = 0o170000
ENCRYPTED_FLAG: Final = 0x1
XLSX_MAIN: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_RELATIONSHIPS: Final = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_RELATIONSHIPS: Final = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)


@dataclass(frozen=True, slots=True)
class Row:
    """One data row (1-based, header excluded) and its structural defect."""

    number: int
    cells: tuple[str, ...]
    defect: str = ""


@dataclass(frozen=True, slots=True)
class Dataset:
    """One tabular dataset: unique headers and bounded rows."""

    name: str
    headers: tuple[str, ...]
    rows: tuple[Row, ...]

    def column(self, header: str) -> int:
        """Return a mapped header's position; mappings are validated first."""
        return self.headers.index(header)


@dataclass(frozen=True, slots=True)
class Attachment:
    """One archive member destined for quarantine, with its scan verdict."""

    path: str
    data: bytes
    sha256: str
    media_type: str
    verdict: str
    reason: str


@dataclass(frozen=True, slots=True)
class CsvOptions:
    """Explicit text decoding options; detection only proposes them."""

    delimiter: str
    encoding: str


def sha256_hex(data: bytes) -> str:
    """Return the lower-case SHA-256 digest used by every import checksum."""
    return hashlib.sha256(data).hexdigest()


def detect_csv(data: bytes) -> CsvOptions:
    """Propose UTF-8 (else Windows-1252) and the dominant header delimiter."""
    encoding = "utf-8"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = _decode(data, "cp1252")
    first = text.split("\n", 1)[0]
    counts = [(first.count(candidate), candidate) for candidate in DELIMITERS]
    best = max(counts, key=lambda item: item[0])
    return CsvOptions(delimiter=best[1] if best[0] else ",", encoding=encoding)


def _decode(data: bytes, encoding: str) -> str:
    if encoding not in ENCODINGS:
        raise ImportInputError(Code.UNSUPPORTED_ENCODING)
    try:
        text = data.decode("utf-8-sig" if encoding == "utf-8" else encoding)
    except UnicodeDecodeError:
        raise ImportInputError(Code.INVALID_ENCODING) from None
    if "\x00" in text:
        raise ImportInputError(Code.INVALID_ENCODING)
    return text


def _headers(raw: list[str]) -> tuple[str, ...]:
    headers = tuple(cell.strip() for cell in raw)
    if (
        not headers
        or len(headers) > MAX_COLUMNS
        or any(not header or len(header) > MAX_HEADER_CHARS for header in headers)
        or len(set(headers)) != len(headers)
    ):
        raise ImportInputError(Code.INVALID_HEADER)
    return headers


def _rows(name: str, records: Iterator[list[str]]) -> Dataset:
    try:
        header = next(records, None)
        if header is None:
            raise ImportInputError(Code.EMPTY_DATASET)
        headers = _headers(header)
        rows: list[Row] = []
        for cells in records:
            if not cells:
                continue
            if len(rows) >= MAX_ROWS:
                raise ImportInputError(Code.TOO_MANY_ROWS)
            number = len(rows) + 1
            defect = ""
            if len(cells) != len(headers):
                defect = "column_count"
            elif any(len(cell) > MAX_CELL_CHARS for cell in cells):
                defect = "too_long"
            rows.append(
                Row(
                    number=number,
                    cells=tuple(cells) if not defect else (),
                    defect=defect,
                )
            )
    except csv.Error:
        raise ImportInputError(Code.MALFORMED_CSV) from None
    return Dataset(name=name, headers=headers, rows=tuple(rows))


def read_csv(name: str, data: bytes, options: CsvOptions) -> Dataset:
    """Read one CSV dataset with strict quoting and bounded shape."""
    if options.delimiter not in DELIMITERS:
        raise ImportInputError(Code.UNSUPPORTED_DELIMITER)
    text = _decode(data, options.encoding)
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=options.delimiter, strict=True
    )
    return _rows(name, iter(reader))


def read_api(data: bytes) -> dict[str, Dataset]:
    """Read ``{"datasets": {"patients": [{column: text|null}]}}`` row objects."""
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise ImportInputError(Code.MALFORMED_JSON) from None
    datasets = document.get("datasets") if isinstance(document, dict) else None
    if (
        not isinstance(datasets, dict)
        or set(datasets) - set(DATASET_FILES)
        or "patients" not in datasets
    ):
        raise ImportInputError(Code.MALFORMED_JSON)
    result: dict[str, Dataset] = {}
    for name, rows in datasets.items():
        if not isinstance(rows, list) or not rows:
            raise ImportInputError(Code.EMPTY_DATASET)
        if len(rows) > MAX_ROWS:
            raise ImportInputError(Code.TOO_MANY_ROWS)
        headers: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ImportInputError(Code.MALFORMED_JSON)
            headers.extend(key for key in row if key not in headers)
        records = [
            headers,
            *(
                [_api_cell(row.get(header)) for header in headers]
                for row in rows
                if isinstance(row, dict)
            ),
        ]
        result[name] = _rows(name, iter(records))
    return result


def _api_cell(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ImportInputError(Code.MALFORMED_JSON)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ImportInputError(Code.MALFORMED_JSON)
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ImportInputError(Code.MALFORMED_JSON)


def _members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ImportInputError(Code.ARCHIVE_LAYOUT)
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            if name != "files/":
                raise ImportInputError(Code.ARCHIVE_LAYOUT)
            continue
        if (
            name in members
            or not (
                name in (MANIFEST, *DATASET_FILES.values())
                or ATTACHMENT_NAME.fullmatch(name)
            )
            or ".." in name
            or info.flag_bits & ENCRYPTED_FLAG
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or (info.external_attr >> 16) & FILE_TYPE_MASK == SYMLINK_MODE
            or info.file_size > MAX_MEMBER_BYTES
            or (
                info.file_size > SMALL_MEMBER_BYTES
                and info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1)
            )
        ):
            raise ImportInputError(Code.ARCHIVE_LAYOUT)
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise ImportInputError(Code.ARCHIVE_LAYOUT)
        members[name] = info
    if MANIFEST not in members or DATASET_FILES["patients"] not in members:
        raise ImportInputError(Code.ARCHIVE_LAYOUT)
    return members


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info) as stream:
        data = stream.read(MAX_MEMBER_BYTES + 1)
    if len(data) != info.file_size:
        raise ImportInputError(Code.ARCHIVE_LAYOUT)
    return data


def _manifest(raw: bytes) -> dict[str, tuple[object, object]]:
    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise ImportInputError(Code.MANIFEST_MISMATCH) from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or manifest.get("v") != ARCHIVE_VERSION:
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    declared: dict[str, tuple[object, object]] = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        path = entry["path"]
        if not isinstance(path, str) or path in declared:
            raise ImportInputError(Code.MANIFEST_MISMATCH)
        declared[path] = (entry["sha256"], entry["bytes"])
    return declared


def open_archive(data: bytes) -> dict[str, bytes]:
    """Return verified member bytes; every member must match the manifest."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = _members(archive)
            contents = {
                name: _read_member(archive, info) for name, info in members.items()
            }
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError):
        raise ImportInputError(Code.MALFORMED_ARCHIVE) from None
    declared = _manifest(contents.pop(MANIFEST))
    if set(declared) != set(contents):
        raise ImportInputError(Code.MANIFEST_MISMATCH)
    for path, member in contents.items():
        digest, size = declared[path]
        if digest != sha256_hex(member) or size != len(member):
            raise ImportConflictError(Code.CHECKSUM_MISMATCH)
    return contents


def scan_attachment(path: str, data: bytes) -> Attachment:
    """Quarantine verdict: allowlisted type, matching signature, no active content."""
    if settings.CLINIC_DATA_MODE != "synthetic":
        # Real uploads need an approved scanning capability (EG-1/EG-5).
        raise ImportInputError(Code.SCAN_UNAVAILABLE)
    suffix = path[path.rfind(".") :].lower() if "." in path else ""
    media = MEDIA_TYPES.get(suffix)
    verdict, reason, media_type = "clean", "", "application/octet-stream"
    if media is None:
        verdict, reason = "rejected", "unsupported_type"
    else:
        media_type, signatures = media
        lowered = data.lower()
        if signatures and not any(data.startswith(sig) for sig in signatures):
            verdict, reason = "rejected", "type_mismatch"
        else:
            for marker, marker_reason in ACTIVE_MARKERS:
                if marker.lower() in lowered:
                    verdict, reason = "rejected", marker_reason
                    break
    return Attachment(
        path=path,
        data=data,
        sha256=sha256_hex(data),
        media_type=media_type,
        verdict=verdict,
        reason=reason,
    )


def _xml(data: bytes) -> ET.Element:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ImportInputError(Code.UNSAFE_SPREADSHEET)
    try:
        return ET.fromstring(data)  # noqa: S314 - DTDs are refused above
    except ET.ParseError:
        raise ImportInputError(Code.MALFORMED_SPREADSHEET) from None


def _cell_index(reference: str, implied: int) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    if not letters:
        index = implied + 1
    else:
        index = 0
        for character in letters.upper():
            index = index * 26 + (ord(character) - ord("A") + 1)
    if not 1 <= index <= MAX_COLUMNS:
        raise ImportInputError(Code.INVALID_HEADER)
    return index - 1


def read_xlsx(data: bytes) -> Dataset:
    """Read the first worksheet's cached text; formula cells are flagged."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            sizes = sum(info.file_size for info in archive.infolist())
            if sizes > MAX_ARCHIVE_BYTES or len(names) > MAX_MEMBERS:
                raise ImportInputError(Code.MALFORMED_SPREADSHEET)
            workbook = _xml(archive.read("xl/workbook.xml"))
            relations = _xml(archive.read("xl/_rels/workbook.xml.rels"))
            shared = (
                _xml(archive.read("xl/sharedStrings.xml"))
                if "xl/sharedStrings.xml" in names
                else None
            )
            sheet_path = _first_sheet(workbook, relations)
            if sheet_path not in names:
                raise ImportInputError(Code.MALFORMED_SPREADSHEET)
            sheet = _xml(archive.read(sheet_path))
    except (zipfile.BadZipFile, KeyError, OSError, EOFError):
        raise ImportInputError(Code.MALFORMED_SPREADSHEET) from None
    strings = _shared_strings(shared)
    parsed: list[tuple[list[str], str]] = []
    for row in sheet.iter(f"{{{XLSX_MAIN}}}row"):
        cells: dict[int, str] = {}
        defect = ""
        position = -1
        for cell in row.findall(f"{{{XLSX_MAIN}}}c"):
            position = _cell_index(cell.get("r", ""), position)
            value, cell_defect = _cell_value(cell, strings)
            cells[position] = value
            defect = defect or cell_defect
        if not cells:
            continue
        if len(parsed) > MAX_ROWS:
            raise ImportInputError(Code.TOO_MANY_ROWS)
        parsed.append(
            ([cells.get(index, "") for index in range(max(cells) + 1)], defect)
        )
    if not parsed:
        raise ImportInputError(Code.EMPTY_DATASET)
    header, header_defect = parsed[0]
    if header_defect:
        raise ImportInputError(Code.INVALID_HEADER)
    width = len(header)
    records = [header] + [
        cells + [""] * (width - len(cells)) for cells, _defect in parsed[1:]
    ]
    dataset = _rows("patients", iter(records))
    rows = tuple(
        Row(number=row.number, cells=(), defect=parsed[row.number][1])
        if parsed[row.number][1] and not row.defect
        else row
        for row in dataset.rows
    )
    return Dataset(name=dataset.name, headers=dataset.headers, rows=rows)


def _first_sheet(workbook: ET.Element, relations: ET.Element) -> str:
    sheet = workbook.find(f"{{{XLSX_MAIN}}}sheets/{{{XLSX_MAIN}}}sheet")
    if sheet is None:
        raise ImportInputError(Code.MALFORMED_SPREADSHEET)
    relation_id = sheet.get(f"{{{XLSX_RELATIONSHIPS}}}id")
    for relation in relations.findall(f"{{{PACKAGE_RELATIONSHIPS}}}Relationship"):
        if relation.get("Id") == relation_id:
            target = relation.get("Target", "")
            target = target.removeprefix("/xl/").removeprefix("/")
            if ".." in target or not target.startswith("worksheets/"):
                raise ImportInputError(Code.MALFORMED_SPREADSHEET)
            return f"xl/{target}"
    raise ImportInputError(Code.MALFORMED_SPREADSHEET)


def _shared_strings(shared: ET.Element | None) -> tuple[str, ...]:
    if shared is None:
        return ()
    return tuple(
        "".join(text.text or "" for text in item.iter(f"{{{XLSX_MAIN}}}t"))
        for item in shared.findall(f"{{{XLSX_MAIN}}}si")
    )


def _cell_value(cell: ET.Element, strings: tuple[str, ...]) -> tuple[str, str]:
    if cell.find(f"{{{XLSX_MAIN}}}f") is not None:
        return "", "formula_cell"
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{XLSX_MAIN}}}t")), ""
    raw = cell.findtext(f"{{{XLSX_MAIN}}}v") or ""
    if kind == "s":
        try:
            return strings[int(raw)], ""
        except (ValueError, IndexError):
            return "", "malformed_cell"
    if kind == "e":
        return "", "malformed_cell"
    return raw, ""


def read_datasets(
    kind: str, data: bytes, options: Mapping[str, CsvOptions] | None = None
) -> dict[str, Dataset]:
    """Read every dataset of one source with explicit per-dataset CSV options."""
    if kind == "csv":
        chosen = (options or {}).get("patients") or detect_csv(data)
        return {"patients": read_csv("patients", data, chosen)}
    if kind == "xlsx":
        return {"patients": read_xlsx(data)}
    if kind == "api":
        return read_api(data)
    if kind == "zip":
        contents = open_archive(data)
        result: dict[str, Dataset] = {}
        for name, member in DATASET_FILES.items():
            if member in contents:
                chosen = (options or {}).get(name) or detect_csv(contents[member])
                result[name] = read_csv(name, contents[member], chosen)
        return result
    raise ImportInputError(Code.UNSUPPORTED_SOURCE)


def archive_attachments(data: bytes) -> tuple[Attachment, ...]:
    """Scan every archive attachment; rejected bytes stay quarantined."""
    contents = open_archive(data)
    return tuple(
        scan_attachment(path, member)
        for path, member in sorted(contents.items())
        if path.startswith("files/")
    )
