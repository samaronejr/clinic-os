from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Never

import pytest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
ADR_DIRECTORY: Final = PROJECT_ROOT / "docs" / "adr"
REQUIRED_HEADINGS: Final = (
    "## Context",
    "## Decision",
    "## Consequences",
    "## Rejected",
    "## Revisit trigger",
)
EXPECTED_IDS: Final = tuple(range(20))
FILENAME_PATTERN: Final = re.compile(r"ADR-([0-9]{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
TITLE_PATTERN: Final = re.compile(r"# ADR-([0-9]{3}): \S")


class AdrIndexError(ValueError):
    pass


def _fail(message: str) -> Never:
    raise AdrIndexError(message)


def _check_record(path: Path) -> int:
    match = FILENAME_PATTERN.fullmatch(path.name)
    if match is None:
        _fail(f"{path.name}: filename must look like ADR-000-short-slug.md")
    adr_id = int(match.group(1))
    lines = path.read_text(encoding="utf-8").splitlines()
    title = TITLE_PATTERN.match(lines[0]) if lines else None
    if title is None or int(title.group(1)) != adr_id:
        _fail(f"{path.name}: first line must be '# ADR-{adr_id:03d}: <title>'")
    for heading in REQUIRED_HEADINGS:
        count = lines.count(heading)
        if count != 1:
            _fail(f"{path.name}: expected '{heading}' once, found {count}")
    return adr_id


def validate_adr_directory(directory: Path) -> tuple[int, ...]:
    """Check every ADR record in the directory; return the sorted ids."""
    records = sorted(directory.glob("*.md"))
    if not records:
        _fail(f"no ADR records in {directory}")
    seen: dict[int, str] = {}
    for path in records:
        adr_id = _check_record(path)
        if adr_id in seen:
            _fail(f"duplicate ADR id {adr_id:03d}: {seen[adr_id]} and {path.name}")
        seen[adr_id] = path.name
    ids = tuple(sorted(seen))
    if ids != tuple(range(len(ids))):
        missing = sorted(set(range(ids[-1] + 1)) - set(ids))
        _fail(f"ADR ids must be contiguous from 000; missing {missing}")
    return ids


def _write_record(
    directory: Path,
    adr_id: int,
    *,
    omit: str | None = None,
    slug: str = "synthetic-decision",
) -> Path:
    body = [f"# ADR-{adr_id:03d}: Synthetic decision", ""]
    for heading in REQUIRED_HEADINGS:
        if heading != omit:
            body += [heading, "", "Synthetic text.", ""]
    path = directory / f"ADR-{adr_id:03d}-{slug}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def test_committed_adrs_are_contiguous_and_complete() -> None:
    # Given: the committed ADR directory.
    # When: every record is checked for filename, title and required headings.
    ids = validate_adr_directory(ADR_DIRECTORY)

    # Then: ADR-000..ADR-019 are present, each exactly once.
    assert ids == EXPECTED_IDS


def test_validator_accepts_a_minimal_valid_set(tmp_path: Path) -> None:
    for adr_id in range(3):
        _write_record(tmp_path, adr_id)

    assert validate_adr_directory(tmp_path) == (0, 1, 2)


@pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
def test_validator_rejects_a_missing_heading(tmp_path: Path, heading: str) -> None:
    _write_record(tmp_path, 0, omit=heading)

    with pytest.raises(AdrIndexError, match=f"expected '{heading}' once, found 0"):
        validate_adr_directory(tmp_path)


def test_validator_rejects_a_repeated_heading(tmp_path: Path) -> None:
    path = _write_record(tmp_path, 0)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Decision\n", encoding="utf-8"
    )

    with pytest.raises(AdrIndexError, match="found 2"):
        validate_adr_directory(tmp_path)


def test_validator_rejects_a_duplicate_id(tmp_path: Path) -> None:
    _write_record(tmp_path, 0)
    _write_record(tmp_path, 0, slug="another-decision")
    _write_record(tmp_path, 1)

    with pytest.raises(AdrIndexError, match="duplicate ADR id 000"):
        validate_adr_directory(tmp_path)


def test_validator_rejects_a_gap_in_ids(tmp_path: Path) -> None:
    for adr_id in (0, 1, 3):
        _write_record(tmp_path, adr_id)

    with pytest.raises(AdrIndexError, match=r"missing \[2\]"):
        validate_adr_directory(tmp_path)


def test_validator_rejects_ids_not_starting_at_zero(tmp_path: Path) -> None:
    _write_record(tmp_path, 1)

    with pytest.raises(AdrIndexError, match=r"missing \[0\]"):
        validate_adr_directory(tmp_path)


@pytest.mark.parametrize(
    ("name", "first_line", "message"),
    [
        ("ADR-5-short.md", "# ADR-005: Synthetic", "filename must look like"),
        ("adr-000-lower.md", "# ADR-000: Synthetic", "filename must look like"),
        ("NOTES.md", "# Notes", "filename must look like"),
        ("ADR-000-title.md", "# ADR-001: Synthetic", "first line must be"),
        ("ADR-000-title.md", "ADR-000: Synthetic", "first line must be"),
        ("ADR-000-title.md", "", "first line must be"),
    ],
)
def test_validator_rejects_malformed_records(
    tmp_path: Path, name: str, first_line: str, message: str
) -> None:
    body = "\n\n".join((first_line, *REQUIRED_HEADINGS))
    (tmp_path / name).write_text(body + "\n", encoding="utf-8")

    with pytest.raises(AdrIndexError, match=message):
        validate_adr_directory(tmp_path)


def test_validator_rejects_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(AdrIndexError, match="no ADR records"):
        validate_adr_directory(tmp_path)
