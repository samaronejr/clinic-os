from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/compliance/applicability-matrix.md"
REGISTER = ROOT / "docs/compliance/ai-risk-register.md"
REVERIFICATION_LOG = ROOT / "docs/compliance/reverification-log.md"
SUCCESSOR = ROOT / "docs/plans/clinic-ops-premium-successor.md"

COLUMNS = (
    "Feature",
    "Controller/operator role",
    "Data categories",
    "Basis candidate",
    "Retention",
    "Transfers",
    "Professional scope",
    "Notices",
    "Approvals",
    "Incident duties",
    "Source URL",
    "Retrieved date",
    "Verification level",
)
AI_CAPABILITIES = frozenset(f"AI-0{number}" for number in range(1, 9))
VERIFICATION_LEVELS = frozenset({"verified", "search-snippet", "unverified"})
AI_REFERENCE = re.compile(r"\bAI-0[1-8]\b")
GATE_REFERENCE = re.compile(r"\bEG-\d+\b")
GATE_TABLE_ROW = re.compile(r"^\| (EG-\d+) \|", re.MULTILINE)
REGISTER_HEADING = re.compile(r"^## (AI-0\d) ", re.MULTILINE)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_matrix(text: str) -> list[dict[str, str]]:
    """Return the matrix rows keyed by column, failing on any malformed row."""
    lines = text.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("|") and tuple(_cells(line)) == COLUMNS
        ),
        None,
    )
    if header_index is None:
        message = "applicability matrix header not found"
        raise ValueError(message)
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = _cells(line)
        if len(cells) != len(COLUMNS) or not all(cells):
            message = f"malformed matrix row with {len(cells)} cells: {line[:60]!r}"
            raise ValueError(message)
        rows.append(dict(zip(COLUMNS, cells, strict=True)))
    if not rows:
        message = "applicability matrix has no rows"
        raise ValueError(message)
    return rows


def _matrix_rows() -> list[dict[str, str]]:
    return parse_matrix(MATRIX.read_text(encoding="utf-8"))


def _contract_gates() -> frozenset[str]:
    gates = frozenset(GATE_TABLE_ROW.findall(SUCCESSOR.read_text(encoding="utf-8")))
    assert gates, "successor contract lists no external gates"
    return gates


def test_every_matrix_row_is_complete_sourced_dated_and_leveled() -> None:
    for row in _matrix_rows():
        feature = row["Feature"]
        assert ISO_DATE.fullmatch(row["Retrieved date"]), feature
        date.fromisoformat(row["Retrieved date"])
        assert row["Verification level"] in VERIFICATION_LEVELS, feature
        assert "https://" in row["Source URL"], feature


def test_every_ai_capability_has_a_feature_row() -> None:
    features = {
        reference
        for row in _matrix_rows()
        for reference in AI_REFERENCE.findall(row["Feature"])
    }
    assert features == AI_CAPABILITIES


def test_every_external_gate_is_mapped_and_no_unknown_gate_is_cited() -> None:
    cited = {
        gate
        for row in _matrix_rows()
        for gate in GATE_REFERENCE.findall(row["Approvals"])
    }
    assert cited == _contract_gates()


def test_rows_below_verified_have_a_reverification_step() -> None:
    log = REVERIFICATION_LOG.read_text(encoding="utf-8")
    for row in _matrix_rows():
        if row["Verification level"] != "verified":
            assert f"| {row['Feature']} |" in log, row["Feature"]


def test_risk_register_has_exactly_one_record_per_ai_capability() -> None:
    headings = REGISTER_HEADING.findall(REGISTER.read_text(encoding="utf-8"))
    assert sorted(headings) == sorted(AI_CAPABILITIES)


def _synthetic_table(cells: list[str]) -> str:
    header = "| " + " | ".join(COLUMNS) + " |"
    separator = "|" + " --- |" * len(COLUMNS)
    row = "| " + " | ".join(cells) + " |"
    return f"{header}\n{separator}\n{row}"


def test_parser_rejects_a_row_missing_columns() -> None:
    with pytest.raises(ValueError, match="malformed matrix row"):
        parse_matrix(_synthetic_table(["AI-01 synthetic", *["x"] * 5]))


def test_parser_rejects_a_row_with_a_blank_cell() -> None:
    cells = ["x"] * len(COLUMNS)
    cells[COLUMNS.index("Retrieved date")] = ""
    with pytest.raises(ValueError, match="malformed matrix row"):
        parse_matrix(_synthetic_table(cells))
