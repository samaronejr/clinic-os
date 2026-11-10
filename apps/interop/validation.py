"""Deterministic row validators: identifiers, dates, units, codes and links.

Validation never guesses: a value either matches its declared format and
closed vocabulary or the row is rejected with a machine reason code. Reasons
name the dataset, row, field and code (plus the operator's own row label);
patient values never enter a report. The same normalized rows feed the
dry-run digest and every commit batch, so the committed dataset is exactly
the approved one.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast
from zoneinfo import ZoneInfo

import rfc8785
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.idempotency import PatientNameValueError, normalize_patient_name
from apps.ehr.longitudinal_values import CODES, observation_value
from apps.intake.demographics import DemographicsInputError, normalize_identifier
from apps.intake.patient_creation import PatientBirthDateError, validate_birth_date
from apps.interop.contract import HISTORY_KINDS, SEX_VALUES
from apps.interop.spreadsheet import is_formula_like

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.identity.models import Clinic
    from apps.interop.sources import Dataset, Row

MAX_SOURCE_ID: Final = 64
MAX_AUTHOR: Final = 150
MAX_TITLE: Final = 160
MAX_NOTE: Final = 20_000
EARLIEST: Final = date(1900, 1, 1)
EXCEL_EPOCH: Final = date(1899, 12, 30)
MAX_EXCEL_SERIAL: Final = 2_958_465
BR_DATE_PARTS: Final = (2, 2, 4)
ISO_DATE_LENGTH: Final = len("2000-01-01")
IDENTIFIER_FIELDS: Final = ("cpf", "cns")
CONTROL: Final = frozenset({"Cc", "Cs"})


@dataclass(frozen=True, slots=True)
class Rejection:
    """One row-level reason; ``source_id`` is the operator's own row label."""

    dataset: str
    row: int
    field: str
    code: str
    source_id: str = ""


@dataclass(frozen=True, slots=True)
class PatientRow:
    """One validated patient row in normalized form."""

    row: int
    source_id: str
    legal_name: str | None
    social_name: str | None
    birth_date: date | None
    sex_at_birth: str | None
    identifiers: tuple[tuple[str, str], ...]
    registered_at: datetime | None
    registered_by: str | None

    def canonical(self) -> dict[str, object]:
        """Return the JSON form bound by the dry-run and batch digests."""
        return {
            "row": self.row,
            "source_id": self.source_id,
            "legal_name": self.legal_name,
            "social_name": self.social_name,
            "birth_date": self.birth_date.isoformat() if self.birth_date else None,
            "sex_at_birth": self.sex_at_birth,
            "identifiers": [list(item) for item in self.identifiers],
            "registered_at": (
                self.registered_at.isoformat() if self.registered_at else None
            ),
            "registered_by": self.registered_by,
        }


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One validated clinical history row bound to an imported patient row."""

    row: int
    source_id: str
    patient_row: int
    kind: str
    recorded_at: datetime
    author: str
    content: dict[str, str | int | None]
    file: str | None

    def canonical(self) -> dict[str, object]:
        """Return the JSON form bound by the dry-run and batch digests."""
        return {
            "row": self.row,
            "source_id": self.source_id,
            "patient_row": self.patient_row,
            "kind": self.kind,
            "recorded_at": self.recorded_at.isoformat(),
            "author": self.author,
            "content": dict(self.content),
            "file": self.file,
        }


@dataclass(frozen=True, slots=True)
class AttachmentFacts:
    """What validation may know about a quarantined archive member."""

    verdict: str
    sha256: str
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Normalized:
    """The complete deterministic validation result of one mapped source."""

    patients: tuple[PatientRow, ...]
    history: tuple[HistoryRow, ...]
    rejections: tuple[Rejection, ...]
    row_counts: dict[str, int]

    @property
    def row_count(self) -> int:
        """Count every source row of every mapped dataset."""
        return sum(self.row_counts.values())

    @property
    def valid_count(self) -> int:
        """Count the rows that a commit will import."""
        return len(self.patients) + len(self.history)


@dataclass(frozen=True, slots=True)
class _Options:
    clinic: Clinic
    zone: ZoneInfo
    date_format: str
    datetime_format: str
    sex_values: Mapping[str, str]
    kind_values: Mapping[str, str]
    required_fields: tuple[str, ...]


class _RowError(Exception):
    def __init__(self, field: str, code: str) -> None:
        self.field = field
        self.code = code
        super().__init__(code)


def _reject(field: str, code: str) -> NoReturn:
    raise _RowError(field, code)


def _text(value: str, *, field: str, maximum: int, required: bool) -> str | None:
    text = " ".join(value.split())
    if not text:
        if required:
            _reject(field, "required")
        return None
    if len(text) > maximum:
        _reject(field, "too_long")
    if any(unicodedata.category(character) in CONTROL for character in text):
        _reject(field, "invalid_text")
    return text


def _required(value: str, *, field: str, maximum: int) -> str:
    return cast("str", _text(value, field=field, maximum=maximum, required=True))


def _name(value: str, *, field: str) -> str | None:
    if not value.strip():
        return None
    try:
        name = normalize_patient_name(value)
    except PatientNameValueError:
        _reject(field, "invalid_name")
    if is_formula_like(name):
        _reject(field, "formula_like")
    return name


def _calendar_date(text: str, date_format: str) -> date | None:
    if date_format == "excel_serial":
        if not text.isdigit() or not 1 <= int(text) <= MAX_EXCEL_SERIAL:
            return None
        return EXCEL_EPOCH + timedelta(days=int(text))
    if date_format == "YYYY-MM-DD":
        candidate = text if len(text) == ISO_DATE_LENGTH else ""
        parts = candidate.split("-")
        numbers = [int(part) for part in parts if part.isdigit()]
        order = (0, 1, 2)
    else:
        parts = text.split("/")
        numbers = [int(part) for part in parts if part.isdigit()]
        order = (2, 1, 0)
        if tuple(len(part) for part in parts) != BR_DATE_PARTS:
            return None
    if len(numbers) != len(BR_DATE_PARTS):
        return None
    try:
        return date(numbers[order[0]], numbers[order[1]], numbers[order[2]])
    except ValueError:
        return None


def parse_date(value: str, date_format: str, *, field: str) -> date:
    """Parse one calendar date exactly in its declared format."""
    parsed = _calendar_date(value.strip(), date_format)
    if parsed is None:
        _reject(field, "invalid_date")
    if parsed < EARLIEST:
        _reject(field, "implausible_date")
    return parsed


def parse_datetime(
    value: str, datetime_format: str, *, field: str, zone: ZoneInfo
) -> datetime:
    """Parse one clinic-local minute and refuse future or implausible times."""
    pattern = (
        "%Y-%m-%dT%H:%M" if datetime_format == "YYYY-MM-DDTHH:MM" else "%d/%m/%Y %H:%M"
    )
    try:
        parsed = datetime.strptime(value.strip(), pattern)  # noqa: DTZ007
    except ValueError:
        _reject(field, "invalid_date")
    aware = parsed.replace(tzinfo=zone)
    if aware.date() < EARLIEST:
        _reject(field, "implausible_date")
    if aware > timezone.now().astimezone(UTC):
        _reject(field, "future_date")
    return aware


def _cells(dataset: Dataset, row: Row, columns: Mapping[str, str]) -> dict[str, str]:
    return {
        field: row.cells[dataset.column(header)] for field, header in columns.items()
    }


def _birth(values: Mapping[str, str], options: _Options) -> date | None:
    if not values.get("birth_date", "").strip():
        return None
    birth = parse_date(values["birth_date"], options.date_format, field="birth_date")
    try:
        validate_birth_date(birth, options.clinic)
    except PatientBirthDateError:
        _reject("birth_date", "future_date")
    return birth


def _identifiers(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    identifiers: list[tuple[str, str]] = []
    for kind in IDENTIFIER_FIELDS:
        raw = values.get(kind, "")
        if raw.strip():
            try:
                identifiers.append((kind, normalize_identifier(kind, raw)))
            except DemographicsInputError:
                _reject(kind, f"invalid_{kind}")
    return tuple(identifiers)


def _patient(
    values: Mapping[str, str], *, number: int, options: _Options
) -> PatientRow:
    source_id = _required(
        values.get("source_id", ""), field="source_id", maximum=MAX_SOURCE_ID
    )
    legal = _name(values.get("legal_name", ""), field="legal_name")
    social = _name(values.get("social_name", ""), field="social_name")
    if legal is None and social is None:
        _reject("legal_name", "required")
    birth = _birth(values, options)
    raw_sex = values.get("sex_at_birth", "").strip()
    sex = options.sex_values.get(raw_sex, raw_sex) if raw_sex else None
    if sex is not None and sex not in SEX_VALUES:
        _reject("sex_at_birth", "invalid_choice")
    registered = values.get("registered_at", "").strip()
    present = {
        "legal_name": legal,
        "social_name": social,
        "birth_date": birth,
        "sex_at_birth": sex,
    }
    missing = [field for field in options.required_fields if present.get(field) is None]
    if missing:
        # Intake would refuse the registration; the import carries no value.
        _reject(missing[0], "required")
    return PatientRow(
        row=number,
        source_id=source_id,
        legal_name=legal,
        social_name=social,
        birth_date=birth,
        sex_at_birth=sex,
        identifiers=_identifiers(values),
        registered_at=(
            parse_datetime(
                registered,
                options.datetime_format,
                field="registered_at",
                zone=options.zone,
            )
            if registered
            else None
        ),
        registered_by=_text(
            values.get("registered_by", ""),
            field="registered_by",
            maximum=MAX_AUTHOR,
            required=False,
        ),
    )


def _note(values: Mapping[str, str]) -> dict[str, str | int | None]:
    text = values.get("text", "")
    if not text.strip():
        _reject("text", "required")
    if len(text) > MAX_NOTE:
        _reject("text", "too_long")
    if any(unicodedata.category(c) in CONTROL and c not in "\n\t" for c in text):
        _reject("text", "invalid_text")
    return {"text": text.replace("\r\n", "\n").strip()}


def _observation(values: Mapping[str, str]) -> dict[str, str | int | None]:
    code = values.get("code", "").strip()
    if code not in CODES:
        _reject("code", "invalid_code")
    unit = values.get("unit", "").strip()
    if unit != CODES[code].unit:
        _reject("unit", "invalid_unit")
    try:
        value = observation_value(
            kind=code, value=values.get("value", "").strip(), unit=unit
        )
    except ValidationError:
        _reject("value", "invalid_value")
    return {"code": code, "value": value, "unit": unit, "code_set_version": 1}


def _attachment(
    values: Mapping[str, str], attachments: Mapping[str, AttachmentFacts]
) -> tuple[dict[str, str | int | None], str]:
    path = values.get("file", "").strip()
    member = f"files/{path}"
    facts = attachments.get(member) if path else None
    if facts is None:
        _reject("file", "missing_file")
    if facts.verdict != "clean":
        _reject("file", "file_rejected")
    title = _text(
        values.get("title", ""), field="title", maximum=MAX_TITLE, required=False
    )
    return {
        "title": title,
        "media_type": facts.media_type,
        "sha256": facts.sha256,
        "size_bytes": facts.size_bytes,
    }, member


def _history(
    values: Mapping[str, str],
    *,
    number: int,
    options: _Options,
    patients: Mapping[str, int],
    attachments: Mapping[str, AttachmentFacts],
) -> HistoryRow:
    source_id = _required(
        values.get("source_id", ""), field="source_id", maximum=MAX_SOURCE_ID
    )
    patient_source = " ".join(values.get("patient_source_id", "").split())
    if patient_source not in patients:
        _reject("patient_source_id", "unknown_patient")
    raw_kind = values.get("kind", "").strip()
    kind = options.kind_values.get(raw_kind, raw_kind)
    if kind not in HISTORY_KINDS:
        _reject("kind", "invalid_kind")
    recorded_at = parse_datetime(
        values.get("recorded_at", ""),
        options.datetime_format,
        field="recorded_at",
        zone=options.zone,
    )
    author = _required(values.get("author", ""), field="author", maximum=MAX_AUTHOR)
    file: str | None = None
    if kind == "note":
        content = _note(values)
    elif kind == "observation":
        content = _observation(values)
    else:
        content, file = _attachment(values, attachments)
    return HistoryRow(
        row=number,
        source_id=source_id,
        patient_row=patients[patient_source],
        kind=kind,
        recorded_at=recorded_at,
        author=author,
        content=content,
        file=file,
    )


def _label(cells: Mapping[str, str]) -> str:
    return " ".join(cells.get("source_id", "").split())[:MAX_SOURCE_ID]


def _patients(
    dataset: Dataset, columns: Mapping[str, str], options: _Options
) -> tuple[list[PatientRow], list[Rejection]]:
    accepted: list[PatientRow] = []
    rejections: list[Rejection] = []
    sources: set[str] = set()
    identifiers: set[tuple[str, str]] = set()
    for row in dataset.rows:
        if row.defect:
            rejections.append(Rejection("patients", row.number, "row", row.defect))
            continue
        cells = _cells(dataset, row, columns)
        try:
            patient = _patient(cells, number=row.number, options=options)
            if patient.source_id in sources:
                _reject("source_id", "duplicate_source_id")
            repeated = [
                kind
                for kind, value in patient.identifiers
                if (kind, value) in identifiers
            ]
            if repeated:
                _reject(repeated[0], "duplicate_identifier")
        except _RowError as error:
            rejections.append(
                Rejection(
                    "patients", row.number, error.field, error.code, _label(cells)
                )
            )
            continue
        sources.add(patient.source_id)
        identifiers.update(patient.identifiers)
        accepted.append(patient)
    return accepted, rejections


def _histories(
    dataset: Dataset,
    columns: Mapping[str, str],
    options: _Options,
    *,
    patients: Mapping[str, int],
    attachments: Mapping[str, AttachmentFacts],
) -> tuple[list[HistoryRow], list[Rejection]]:
    accepted: list[HistoryRow] = []
    rejections: list[Rejection] = []
    sources: set[str] = set()
    for row in dataset.rows:
        if row.defect:
            rejections.append(Rejection("history", row.number, "row", row.defect))
            continue
        cells = _cells(dataset, row, columns)
        try:
            entry = _history(
                cells,
                number=row.number,
                options=options,
                patients=patients,
                attachments=attachments,
            )
            if entry.source_id in sources:
                _reject("source_id", "duplicate_source_id")
        except _RowError as error:
            rejections.append(
                Rejection("history", row.number, error.field, error.code, _label(cells))
            )
            continue
        sources.add(entry.source_id)
        accepted.append(entry)
    return accepted, rejections


def normalize(
    datasets: Mapping[str, Dataset],
    mapping: Mapping[str, object],
    *,
    clinic: Clinic,
    attachments: Mapping[str, AttachmentFacts],
    required_fields: tuple[str, ...],
) -> Normalized:
    """Validate every mapped row; rejected rows keep reasons, never values."""
    mapped = cast("dict[str, dict[str, dict[str, object]]]", mapping["datasets"])
    patient_values = cast(
        "dict[str, dict[str, str]]", mapped["patients"].get("values", {})
    )
    history_values = (
        cast("dict[str, dict[str, str]]", mapped["history"].get("values", {}))
        if "history" in mapped
        else {}
    )
    options = _Options(
        clinic=clinic,
        zone=ZoneInfo(str(clinic.timezone)),
        date_format=str(mapping.get("date_format", "DD/MM/YYYY")),
        datetime_format=str(mapping.get("datetime_format", "DD/MM/YYYY HH:MM")),
        sex_values=patient_values.get("sex_at_birth", {}),
        kind_values=history_values.get("kind", {}),
        required_fields=required_fields,
    )
    patients, rejections = _patients(
        datasets["patients"],
        cast("dict[str, str]", mapped["patients"]["columns"]),
        options,
    )
    counts = {"patients": len(datasets["patients"].rows)}
    history: list[HistoryRow] = []
    if "history" in mapped and "history" in datasets:
        counts["history"] = len(datasets["history"].rows)
        history, refused = _histories(
            datasets["history"],
            cast("dict[str, str]", mapped["history"]["columns"]),
            options,
            patients={patient.source_id: patient.row for patient in patients},
            attachments=attachments,
        )
        rejections += refused
    return Normalized(
        patients=tuple(patients),
        history=tuple(history),
        rejections=tuple(rejections),
        row_counts=counts,
    )


def canonical_bytes(value: object) -> bytes:
    """Encode one JSON-shaped value canonically (RFC 8785); floats never occur."""
    return rfc8785.dumps(cast("Any", value))


def rows_digest(rows: list[dict[str, object]]) -> str:
    """Hash one ordered list of canonical rows (RFC 8785, SHA-256)."""
    return hashlib.sha256(canonical_bytes(rows)).hexdigest()


def dataset_digest(
    normalized: Normalized, *, source_sha256: str, mapping_sha256: str
) -> str:
    """Bind the exact source, mapping, accepted rows and rejections."""
    return hashlib.sha256(
        canonical_bytes(
            {
                "source_sha256": source_sha256,
                "mapping_sha256": mapping_sha256,
                "patients": [row.canonical() for row in normalized.patients],
                "history": [row.canonical() for row in normalized.history],
                "rejections": [
                    [item.dataset, item.row, item.field, item.code]
                    for item in normalized.rejections
                ],
            }
        )
    ).hexdigest()
