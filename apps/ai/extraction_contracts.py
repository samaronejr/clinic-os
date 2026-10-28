"""Untrusted region proposals and deliberately conservative pt-BR parsers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Final

MAX_CANDIDATES: Final = 128
BBOX_LENGTH: Final = 4
COORDINATE_SCALE: Final = 10000
UNITS: Final = frozenset(
    {
        "mg/dL",
        "g/dL",
        "mmol/L",
        "mEq/L",
        "U/L",
        "ng/mL",
        "pg/mL",
        "mcg/L",
        "\u00b5g/L",
        "10^3/\u00b5L",
        "10^6/\u00b5L",
        "%",
        "mL",
        "L",
        "kg",
        "g",
        "cm",
        "m",
        "mmHg",
        "bpm",
        "kg/m2",
        "1",
    }
)


class ExtractionInputError(ValueError):
    """Never include source content or record selectors in an exception."""

    def __init__(self) -> None:
        """Use one content-free error for malformed documents and proposals."""
        super().__init__("invalid extraction data")


def parse_decimal(raw: str) -> str | None:
    """Require decimal comma; dots require explicit groups and a decimal comma."""
    if (
        re.fullmatch(
            r"[+-]?(?:(?:0|[1-9][0-9]*)(?:,[0-9]+)?|"
            r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+,[0-9]+)",
            raw,
        )
        is None
    ):
        return None
    return raw.replace(".", "").replace(",", ".")


def parse_date(raw: str) -> str | None:
    """Convert an exact DD/MM/YYYY date, without repairing missing information."""
    if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", raw) is None:
        return None
    try:
        return date(int(raw[6:]), int(raw[3:5]), int(raw[:2])).isoformat()
    except ValueError:
        return None


def parse_unit(raw: str) -> str | None:
    """Keep only an explicit known unit, with no case folding or conversion."""
    return raw if raw in UNITS else None


@dataclass(frozen=True, slots=True)
class Region:
    """One-based page and unrotated top-left normalized integer bounding box."""

    page: int
    bbox: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        """Refuse zero-area, reversed, fractional, or out-of-page anchors."""
        if (
            type(self.page) is not int
            or self.page < 1
            or len(self.bbox) != BBOX_LENGTH
            or any(
                type(v) is not int or not 0 <= v <= COORDINATE_SCALE for v in self.bbox
            )
            or self.bbox[0] >= self.bbox[2]
            or self.bbox[1] >= self.bbox[3]
        ):
            raise ExtractionInputError

    def display_bbox(self, rotation: int) -> tuple[int, int, int, int]:
        """Apply the source's clockwise rotation to the page and its highlight."""
        left, top, right, bottom = self.bbox
        match rotation:
            case 0:
                return self.bbox
            case 90:
                return 10000 - bottom, left, 10000 - top, right
            case 180:
                return 10000 - right, 10000 - bottom, 10000 - left, 10000 - top
            case 270:
                return top, 10000 - right, bottom, 10000 - left
            case _:
                raise ExtractionInputError


@dataclass(frozen=True, slots=True, repr=False)
class ExtractionCandidate:
    """Draft DTO; raw text, exact source value, and parsed string stay distinct."""

    field: str
    kind: str
    raw_text: str
    value: str
    parsed_value: str
    unit: str
    reference_interval: str
    region: Region
    status: str = "candidate"
    reason: str = ""

    def payload(self) -> dict[str, object]:
        """Serialize only into the purpose-bound encrypted candidate envelope."""
        payload = asdict(self)
        payload["region"] = {"page": self.region.page, "bbox": list(self.region.bbox)}
        return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionInputError
        result[key] = value
    return result


def parse_proposals(
    content: str, *, rotations: tuple[int, ...]
) -> tuple[ExtractionCandidate, ...]:
    """Bound model proposals, then parse locally; model-supplied parses are refused."""
    if not rotations or any(r not in (0, 90, 180, 270) for r in rotations):
        raise ExtractionInputError
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as error:
        raise ExtractionInputError from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"candidates"}
        or not isinstance(payload["candidates"], list)
        or not 1 <= len(payload["candidates"]) <= MAX_CANDIDATES
    ):
        raise ExtractionInputError
    return tuple(_candidate(row, pages=len(rotations)) for row in payload["candidates"])


def _candidate(row: object, *, pages: int) -> ExtractionCandidate:
    if (
        not isinstance(row, dict)
        or set(row)
        != {
            "field",
            "kind",
            "raw_text",
            "value",
            "unit",
            "reference_interval",
            "page",
            "bbox",
            "legible",
        }
        or any(
            type(row[key]) is not str or len(row[key]) > limit
            for key, limit in (
                ("field", 32),
                ("kind", 12),
                ("raw_text", 1024),
                ("value", 128),
                ("unit", 64),
                ("reference_interval", 256),
            )
        )
        or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", row["field"]) is None
        or row["kind"] not in ("decimal", "date", "text")
        or type(row["legible"]) is not bool
        or not isinstance(row["bbox"], list)
        or type(row["page"]) is not int
        or not 1 <= row["page"] <= pages
    ):
        raise ExtractionInputError
    region = Region(row["page"], tuple(row["bbox"]))
    reason = ""
    parsed: str | None = None
    if not row["legible"]:
        reason = "unreadable"
    elif row["kind"] == "text":
        reason = "unstructured"
    elif row["kind"] == "date":
        parsed = parse_date(row["value"]) if not row["unit"] else None
    elif parse_unit(row["unit"]) is None:
        reason = "unit_unresolved"
    else:
        parsed = parse_decimal(row["value"])
    if not reason and (
        parsed is None
        or row["value"] not in row["raw_text"]
        or (row["unit"] and row["unit"] not in row["raw_text"])
        or (
            row["reference_interval"]
            and row["reference_interval"] not in row["raw_text"]
        )
    ):
        reason = "value_unresolved"
    return ExtractionCandidate(
        field=row["field"],
        kind=row["kind"],
        raw_text=row["raw_text"],
        value=row["value"],
        parsed_value=parsed if not reason and parsed else "",
        unit=row["unit"],
        reference_interval=row["reference_interval"],
        region=region,
        status="ambiguous" if reason else "candidate",
        reason=reason,
    )


def filing_payload(
    candidate: ExtractionCandidate, *, confirmer: str
) -> dict[str, object]:
    """Only an explicit confirmation may turn an unambiguous draft into a fact."""
    if candidate.status != "candidate" or not candidate.parsed_value or not confirmer:
        raise ExtractionInputError
    return {
        "code": candidate.field,
        "value": candidate.value,
        "parsed_value": candidate.parsed_value,
        "unit": candidate.unit,
        "reference_interval": candidate.reference_interval,
        "abnormal_flag": "",
        "provenance": "imported",
        "confirmer": confirmer,
        "page": candidate.region.page,
        "bbox": list(candidate.region.bbox),
        "raw_text": candidate.raw_text,
    }
