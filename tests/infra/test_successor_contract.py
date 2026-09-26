from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final, Never

import pytest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PLANS: Final = PROJECT_ROOT / "docs" / "plans"
LEDGER: Final = PLANS / "clinic-ops-premium-successor.supersession.json"
FROZEN_PLAN: Final = PLANS / "clinic-os-phase1a-approved.md"
FROZEN_SIDECAR: Final = FROZEN_PLAN.with_suffix(".sha256")

FIELDS: Final = frozenset(
    {"id", "source_path", "source_lines", "class", "disposition", "replaced_by_todo"}
)
CLASSES: Final = frozenset({1, 2, 3})
SAFETY_CLASS: Final = 3
DISPOSITIONS: Final = frozenset(
    {
        "ADDED",
        "CONDITIONALLY_SUPERSEDED",
        "EXTENDED",
        "PRESERVED",
        "REPLACED",
        "REWRITTEN",
        "SUPERSEDED",
    }
)
SAFETY_DISPOSITIONS: Final = frozenset({"ADDED", "PRESERVED"})
FIRST_TODO: Final = 1
LAST_TODO: Final = 78
# Machine data copied from the draft's supersession map: the JSON splits the
# mixed SD-8 row into SD-8a/SD-8b, and these rows carry the class-3 obligations.
EXPECTED_SD_IDS: Final = frozenset(
    {f"SD-{number}" for number in range(1, 18) if number != 8} | {"SD-8a", "SD-8b"}
)
CLASS_THREE_SD_IDS: Final = frozenset(
    {"SD-2", "SD-3", "SD-8b", "SD-13", "SD-14", "SD-17"}
)
ID_PATTERN: Final = re.compile(r"SD-([1-9][0-9]*)[a-z]?")
LINE_SPAN: Final = r"[1-9][0-9]*(?:-[1-9][0-9]*)?"
LINES_PATTERN: Final = re.compile(rf"{LINE_SPAN}(?:,{LINE_SPAN})*")


class SuccessorLedgerError(ValueError):
    pass


def _fail(message: str) -> Never:
    raise SuccessorLedgerError(message)


def _source_file(
    root: Path, row_id: str, value: object, tracked: frozenset[str] | None
) -> Path:
    if not isinstance(value, str) or not value:
        _fail(f"{row_id}: source_path must be a non-empty string")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        _fail(f"{row_id}: source_path must stay inside the repository")
    if tracked is not None and relative.as_posix() not in tracked:
        _fail(f"{row_id}: source_path is not tracked by Git: {value}")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        _fail(f"{row_id}: source_path is missing: {value}")
    return path


def _check_lines(path: Path, row_id: str, value: object) -> None:
    if not isinstance(value, str) or LINES_PATTERN.fullmatch(value) is None:
        _fail(f"{row_id}: source_lines must look like '12' or '3-9,14'")
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    for span in value.split(","):
        start_text, _, end_text = span.partition("-")
        start = int(start_text)
        end = int(end_text) if end_text else start
        if start > end or end > line_count:
            _fail(f"{row_id}: source_lines {span} outside 1-{line_count}")


def _check_todos(row_id: str, value: object) -> None:
    if not isinstance(value, list) or not value:
        _fail(f"{row_id}: replaced_by_todo must be a non-empty list")
    for todo in value:
        valid = type(todo) is int and FIRST_TODO <= todo <= LAST_TODO
        if not valid:
            _fail(f"{row_id}: replaced_by_todo has an unknown todo {todo!r}")


def _disposition_keyword(row_id: str, value: object) -> str:
    if not isinstance(value, str):
        _fail(f"{row_id}: disposition must be a string")
    keyword, separator, detail = value.partition(": ")
    if keyword not in DISPOSITIONS or (separator and not detail.strip()):
        _fail(f"{row_id}: unknown disposition {value!r}")
    return keyword


def _check_row(
    root: Path, row: object, seen: set[str], tracked: frozenset[str] | None
) -> str:
    if not isinstance(row, dict) or set(row) != FIELDS:
        _fail(f"ledger row must have exactly the fields {sorted(FIELDS)}")
    row_id = row["id"]
    if not isinstance(row_id, str) or ID_PATTERN.fullmatch(row_id) is None:
        _fail(f"malformed SD id: {row_id!r}")
    if row_id in seen:
        _fail(f"duplicate SD id: {row_id}")
    seen.add(row_id)
    source = _source_file(root, row_id, row["source_path"], tracked)
    _check_lines(source, row_id, row["source_lines"])
    row_class = row["class"]
    if type(row_class) is not int or row_class not in CLASSES:
        _fail(f"{row_id}: class must be 1, 2 or 3")
    disposition = _disposition_keyword(row_id, row["disposition"])
    if row_class == SAFETY_CLASS and disposition not in SAFETY_DISPOSITIONS:
        _fail(f"{row_id}: a class-3 obligation can only be PRESERVED or ADDED")
    _check_todos(row_id, row["replaced_by_todo"])
    return row_id


def validate_ledger(
    raw: str, root: Path, tracked: frozenset[str] | None = None
) -> tuple[str, ...]:
    """Parse and check the ledger; return its SD ids in file order.

    With ``tracked``, every source_path must also be a Git-tracked file.
    """
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        _fail("ledger must be a non-empty JSON list")
    seen: set[str] = set()
    return tuple(_check_row(root, row, seen, tracked) for row in rows)


def _tracked_files(root: Path) -> frozenset[str]:
    git = shutil.which("git")
    assert git is not None, "Git is required to prove ledger sources are tracked"
    result = subprocess.run(  # noqa: S603 - fixed read-only Git metadata query.
        (git, "-C", str(root), "ls-files", "-z"),
        check=True,
        capture_output=True,
    )
    return frozenset(result.stdout.decode("utf-8").split("\0")) - {""}


def _committed_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = json.loads(LEDGER.read_text(encoding="utf-8"))
    return rows


def _write_repo_with(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    for row in rows:
        source = tmp_path / str(row["source_path"])
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("synthetic line\n" * 200, encoding="utf-8")
    return tmp_path


def _valid_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "SD-1",
        "source_path": "docs/historical.md",
        "source_lines": "3-9",
        "class": 1,
        "disposition": "SUPERSEDED",
        "replaced_by_todo": [1],
    }
    row.update(overrides)
    return row


def test_committed_ledger_is_valid_and_covers_every_sd_entry() -> None:
    # Given: the committed successor ledger and the repository it cites.
    raw = LEDGER.read_text(encoding="utf-8")

    # When: the ledger is parsed and every row is checked against tracked files.
    ids = validate_ledger(raw, PROJECT_ROOT, _tracked_files(PROJECT_ROOT))

    # Then: exactly the draft's SD ids are present, each once.
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_SD_IDS


def test_committed_class_three_rows_are_pinned_and_preserved_or_added() -> None:
    # Given: the committed rows carrying a safety, security or provenance class.
    safety = [row for row in _committed_rows() if row["class"] == SAFETY_CLASS]

    # Then: no draft class-3 row was demoted or added silently...
    assert {row["id"] for row in safety} == CLASS_THREE_SD_IDS
    # ...and none of them is weakened.
    for row in safety:
        keyword = _disposition_keyword(str(row["id"]), row["disposition"])
        assert keyword in SAFETY_DISPOSITIONS


def test_frozen_phase1a_plan_digest_matches_its_sidecar() -> None:
    # Given: the frozen Phase 1A plan bytes and their committed sidecar.
    plan_bytes = FROZEN_PLAN.read_bytes()
    sidecar = FROZEN_SIDECAR.read_bytes()

    # Then: the successor contract left both byte-identical to each other.
    assert sidecar == f"{hashlib.sha256(plan_bytes).hexdigest()}\n".encode()


def test_validator_accepts_a_minimal_valid_ledger(tmp_path: Path) -> None:
    rows = [_valid_row(), _valid_row(id="SD-2", **{"class": 3}, disposition="ADDED")]
    root = _write_repo_with(tmp_path, rows)

    assert validate_ledger(json.dumps(rows), root) == ("SD-1", "SD-2")


def test_validator_rejects_a_missing_source_path(tmp_path: Path) -> None:
    rows = [_valid_row()]
    root = _write_repo_with(tmp_path, rows)
    rows[0]["source_path"] = "docs/absent.md"

    with pytest.raises(SuccessorLedgerError, match="source_path is missing"):
        validate_ledger(json.dumps(rows), root)


def test_validator_rejects_an_untracked_source_path(tmp_path: Path) -> None:
    rows = [_valid_row()]
    root = _write_repo_with(tmp_path, rows)

    with pytest.raises(SuccessorLedgerError, match="not tracked"):
        validate_ledger(json.dumps(rows), root, frozenset({"docs/other.md"}))


def test_validator_accepts_a_tracked_source_path_and_disposition_detail(
    tmp_path: Path,
) -> None:
    rows = [_valid_row(disposition="SUPERSEDED: by the successor contract")]
    root = _write_repo_with(tmp_path, rows)

    ids = validate_ledger(json.dumps(rows), root, frozenset({"docs/historical.md"}))

    assert ids == ("SD-1",)


def test_validator_rejects_a_duplicate_sd_id(tmp_path: Path) -> None:
    rows = [_valid_row(), _valid_row()]
    root = _write_repo_with(tmp_path, rows)

    with pytest.raises(SuccessorLedgerError, match="duplicate SD id"):
        validate_ledger(json.dumps(rows), root)


@pytest.mark.parametrize(
    "disposition", ["SUPERSEDED", "REPLACED", "REWRITTEN", "REPLACED: by a stub"]
)
def test_validator_rejects_a_weakened_class_three_row(
    tmp_path: Path, disposition: str
) -> None:
    rows = [_valid_row(**{"class": 3}, disposition=disposition)]
    root = _write_repo_with(tmp_path, rows)

    with pytest.raises(SuccessorLedgerError, match="class-3"):
        validate_ledger(json.dumps(rows), root)


def test_validator_rejects_malformed_json(tmp_path: Path) -> None:
    with pytest.raises(json.JSONDecodeError):
        validate_ledger('[{"id": "SD-1",]', tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"source_lines": "180-240"}, "outside"),
        ({"source_lines": "9-3"}, "outside"),
        ({"source_lines": "L3"}, "source_lines"),
        ({"source_path": "../escape.md"}, "inside the repository"),
        ({"id": "SD-01"}, "malformed SD id"),
        ({"class": "3"}, "class must be"),
        ({"disposition": "DROPPED"}, "unknown disposition"),
        ({"disposition": "DROPPED: gone"}, "unknown disposition"),
        ({"disposition": "SUPERSEDED: "}, "unknown disposition"),
        ({"disposition": 3}, "disposition must be a string"),
        ({"replaced_by_todo": []}, "non-empty list"),
        ({"replaced_by_todo": [79]}, "unknown todo"),
        ({"replaced_by_todo": [True]}, "unknown todo"),
    ],
)
def test_validator_rejects_malformed_rows(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    root = _write_repo_with(tmp_path, [_valid_row()])
    rows = [_valid_row(**overrides)]

    with pytest.raises(SuccessorLedgerError, match=message):
        validate_ledger(json.dumps(rows), root)


@pytest.mark.parametrize("raw", ["[]", "{}", '[{"id": "SD-1"}]'])
def test_validator_rejects_wrong_shapes(tmp_path: Path, raw: str) -> None:
    with pytest.raises(SuccessorLedgerError):
        validate_ledger(raw, tmp_path)
