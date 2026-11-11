"""Trace every threat-model mitigation to the tests that prove it (todo 73).

Each ``docs/threat-models/*.md`` keeps exactly one mitigation table whose header
is ``TABLE_HEADER``. The last column lists pytest node ids, each in backticks and
separated by ``<br>``; the first id outside ``tests/renewal/browser/`` is the
row's designated proof. A mitigation is ``covered`` only from execution
evidence, never from definitions:

* it names at least one test id, every id resolves to a collected test function
  in ``tests/`` (static AST lookup, no import), and at least one id is outside
  ``tests/renewal/browser/`` (those skip outside the browser runner, so they can
  only add evidence, never be the sole proof);
* the JUnit reports of a run (``--junit``, required and repeatable) show no
  mapped test failed or errored and at least one mapped non-browser test
  passed. Skipped and not-run tests never count.

Everything else is ``uncovered``. A mitigation whose text contains
``**Gap:**`` names a control known to be missing (state ``gap``); one whose
text contains ``**Unproved:**`` names a clause no mapped test proves (state
``partial``). Neither counts as covered, both are listed in every report, and
``report`` exits 0 only when every mitigation is ``covered``. ``inventory``
only checks the mapping (``mapped``/``unmapped``) and never says covered::

    python -m ops.testing.threat_model_coverage report --junit XML [...] --output PATH
    python -m ops.testing.threat_model_coverage inventory --output PATH
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, TypeGuard
from xml.etree import ElementTree as ET

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.isolation_common import IsolationError, canonical_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ops.testing.isolation_common import JsonObject, JsonValue

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
MODEL_DIRECTORY: Final = "docs/threat-models"
TABLE_HEADER: Final = "| ID | STRIDE | Threat | Mitigation | Todos | Tests |"
TABLE_RULE: Final = "| --- | --- | --- | --- | --- | --- |"
BROWSER_PREFIX: Final = "tests/renewal/browser/"
STRIDE: Final = {
    "S": "Spoofing",
    "T": "Tampering",
    "R": "Repudiation",
    "I": "Information disclosure",
    "D": "Denial of service",
    "E": "Elevation of privilege",
}
MITIGATION_ID: Final = re.compile(r"[A-Z]{2}-([STRIDE])[1-9][0-9]*")
NODE_ID: Final = re.compile(
    r"tests/[a-z0-9_/]+\.py::(?:Test[A-Za-z0-9_]+::)?test_[A-Za-z0-9_]+"
)
TODOS: Final = re.compile(r"[1-9][0-9]*(?:, [1-9][0-9]*)*")
COLUMNS: Final = 6
GAP_MARKER: Final = "**Gap:**"
UNPROVED_MARKER: Final = "**Unproved:**"
MARKERS: Final = (GAP_MARKER, UNPROVED_MARKER)
STATES: Final = ("covered", "partial", "gap", "uncovered")


@dataclass(frozen=True)
class Mitigation:
    """One row of a threat model's mitigation table."""

    model: str
    identifier: str
    mitigation: str
    todos: tuple[int, ...]
    tests: tuple[str, ...]
    gap: str | None = None
    unproved: str | None = None


def _fail(message: str) -> Never:
    raise IsolationError(message)


def parse_threat_model(path: Path) -> tuple[Mitigation, ...]:
    """Return every mitigation row of one threat model, refusing drift."""
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line == TABLE_HEADER]
    if len(starts) != 1:
        _fail(f"{path.name} needs exactly one mitigation table header")
    start = starts[0]
    if start + 1 >= len(lines) or lines[start + 1] != TABLE_RULE:
        _fail(f"{path.name} mitigation table has no exact rule line")
    rows: list[Mitigation] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(_row(path.stem, line))
    if not rows:
        _fail(f"{path.name} mitigation table is empty")
    for line in lines[start + 2 + len(rows) :]:
        if MITIGATION_ID.match(line.removeprefix("| ")):
            _fail(f"{path.name} has a mitigation row outside its table")
    return tuple(rows)


def _row(model: str, line: str) -> Mitigation:
    if not line.endswith(" |"):
        _fail(f"{model} row is not a closed table row")
    cells = [cell.strip() for cell in line[1:-1].split(" | ")]
    if len(cells) != COLUMNS:
        _fail(f"{model} row has {len(cells)} cells, expected {COLUMNS}")
    identifier, stride, _threat, mitigation, todos, tests = cells
    match = MITIGATION_ID.fullmatch(identifier)
    if match is None:
        _fail(f"{model} row id {identifier!r} is malformed")
    if STRIDE[match.group(1)] != stride:
        _fail(f"{identifier} STRIDE cell does not match its id letter")
    if not mitigation:
        _fail(f"{identifier} has no mitigation text")
    if TODOS.fullmatch(todos) is None:
        _fail(f"{identifier} todo cell is malformed")
    return Mitigation(
        model=model,
        identifier=identifier,
        mitigation=mitigation,
        todos=tuple(int(item) for item in todos.split(", ")),
        tests=_test_ids(identifier, tests),
        gap=_clause(identifier, mitigation, GAP_MARKER),
        unproved=_clause(identifier, mitigation, UNPROVED_MARKER),
    )


def _clause(identifier: str, mitigation: str, marker: str) -> str | None:
    """Return the text after ``marker`` up to the next marker, if present."""
    if marker not in mitigation:
        return None
    if mitigation.count(marker) != 1:
        _fail(f"{identifier} repeats {marker}")
    text = mitigation.split(marker, 1)[1]
    for other in MARKERS:
        text = text.split(other, 1)[0]
    if not text.strip():
        _fail(f"{identifier} has an empty {marker} clause")
    return text.strip()


def _test_ids(identifier: str, cell: str) -> tuple[str, ...]:
    if not cell:
        return ()
    ids: list[str] = []
    for part in cell.split("<br>"):
        node = part.removeprefix("`").removesuffix("`")
        if part != f"`{node}`":
            _fail(f"{identifier} test cell must list backticked node ids")
        if NODE_ID.fullmatch(node) is None:
            _fail(f"{identifier} test id {node!r} is not a pytest node id")
        if node in ids:
            _fail(f"{identifier} repeats test id {node}")
        ids.append(node)
    return tuple(ids)


def load_mitigations(root: Path = PROJECT_ROOT) -> tuple[Mitigation, ...]:
    """Parse every threat model and refuse a duplicate mitigation id."""
    paths = sorted((root / MODEL_DIRECTORY).glob("*.md"))
    if not paths:
        _fail("no threat model is tracked")
    rows = tuple(row for path in paths for row in parse_threat_model(path))
    seen: set[str] = set()
    for row in rows:
        if row.identifier in seen:
            _fail(f"mitigation id {row.identifier} appears twice")
        seen.add(row.identifier)
    return rows


def defined_tests(root: Path = PROJECT_ROOT) -> frozenset[str]:
    """Return the node ids of every statically defined test function."""
    found: set[str] = set()
    for path in sorted((root / "tests").rglob("test_*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in tree.body:
            if _is_test(node):
                found.add(f"{relative}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                found.update(
                    f"{relative}::{node.name}::{member.name}"
                    for member in node.body
                    if _is_test(member)
                )
    return frozenset(found)


def _is_test(node: ast.stmt) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
        node.name.startswith("test")
    )


def executed_outcomes(junit_paths: Iterable[Path]) -> dict[str, str]:
    """Fold pytest JUnit reports into one outcome per test function id.

    Parametrized variants fold into their function id: any failure or error
    fails it, otherwise any pass passes it, otherwise it was skipped. Several
    reports (shards, phases) merge the same way.
    """
    variants: dict[str, list[str]] = {}
    paths = list(junit_paths)
    if not paths:
        _fail("at least one JUnit report is required")
    for path in paths:
        root = ET.parse(path).getroot()  # noqa: S314 - local pytest output
        cases = list(root.iter("testcase"))
        if not cases:
            _fail(f"{path.name} records no test case")
        for case in cases:
            nodeid = _junit_nodeid(case.get("classname", ""), case.get("name", ""))
            variants.setdefault(nodeid, []).append(_case_outcome(case))
    return {
        nodeid: (
            "failed"
            if "failed" in outcomes
            else "passed"
            if "passed" in outcomes
            else "skipped"
        )
        for nodeid, outcomes in variants.items()
    }


def _junit_nodeid(classname: str, name: str) -> str:
    parts = classname.split(".")
    if not name or len(parts) < 2 or parts[0] != "tests":  # noqa: PLR2004
        _fail(f"JUnit case {classname!r}::{name!r} is not a tests/ node")
    owner = [parts.pop()] if parts[-1].startswith("Test") else []
    return "::".join(("/".join(parts) + ".py", *owner, name.split("[", 1)[0]))


def _case_outcome(case: ET.Element) -> str:
    tags = {child.tag for child in case}
    if tags & {"failure", "error"}:
        return "failed"
    if "skipped" in tags:
        return "skipped"
    return "passed"


def mapping_inventory(
    mitigations: Iterable[Mitigation], index: frozenset[str]
) -> JsonObject:
    """Return which mitigations name usable tests; never a coverage verdict.

    A row is ``mapped`` when every id is a defined test and one is outside the
    browser suites. Mapping proves nothing ran: only ``coverage_report`` with
    execution outcomes can call a mitigation covered.
    """
    rows: list[JsonValue] = []
    unmapped: list[JsonValue] = []
    for row in mitigations:
        reasons = _mapping_reasons(row, index)
        if reasons:
            unmapped.append(row.identifier)
        rows.append(
            {
                "id": row.identifier,
                "model": row.model,
                "state": "unmapped" if reasons else "mapped",
                "gap": row.gap,
                "unproved": row.unproved,
                "reasons": list(reasons),
                "proof": proof_test(row),
            }
        )
    return {
        "schema": "clinic-threat-mapping-v1",
        "mitigations": len(rows),
        "mapped": len(rows) - len(unmapped),
        "unmapped": unmapped,
        "partial": _marked(rows, "unproved"),
        "gaps": _marked(rows, "gap"),
        "rows": rows,
    }


def proof_test(row: Mitigation) -> str | None:
    """Return the row's designated proof: its first non-browser test id."""
    return next((node for node in row.tests if not _browser(node)), None)


def coverage_report(
    mitigations: Iterable[Mitigation],
    index: frozenset[str],
    outcomes: Mapping[str, str],
) -> JsonObject:
    """Return each mitigation's state for one run; only ``covered`` is closure.

    ``uncovered`` (no passing proof, or a mapped test failed) dominates; then
    ``gap`` (a ``**Gap:**`` control is missing) and ``partial`` (an
    ``**Unproved:**`` clause has no proof). Neither counts as covered.
    """
    rows: list[JsonValue] = []
    states: dict[str, list[JsonValue]] = {state: [] for state in STATES}
    for row in mitigations:
        reasons, tests = _assess(row, index, outcomes)
        state = (
            "uncovered"
            if reasons
            else "gap"
            if row.gap is not None
            else "partial"
            if row.unproved is not None
            else "covered"
        )
        states[state].append(row.identifier)
        rows.append(
            {
                "id": row.identifier,
                "model": row.model,
                "todos": list(row.todos),
                "state": state,
                "gap": row.gap,
                "unproved": row.unproved,
                "reasons": list(reasons),
                "tests": tests,
            }
        )
    return {
        "schema": "clinic-threat-coverage-v3",
        "execution": "junit",
        "mitigations": len(rows),
        "covered": len(states["covered"]),
        "partial": states["partial"],
        "gaps": states["gap"],
        "uncovered": states["uncovered"],
        "complete": len(states["covered"]) == len(rows),
        "rows": rows,
    }


def _mapping_reasons(row: Mitigation, index: frozenset[str]) -> list[str]:
    reasons = [
        f"{node} is not a defined test" for node in row.tests if node not in index
    ]
    if not row.tests:
        reasons.append("no test id is mapped")
    elif proof_test(row) is None:
        reasons.append("only browser tests are mapped; they skip outside the runner")
    return reasons


def _assess(
    row: Mitigation,
    index: frozenset[str],
    outcomes: Mapping[str, str],
) -> tuple[list[str], list[JsonValue]]:
    reasons = _mapping_reasons(row, index)
    tests: list[JsonValue] = []
    for node in row.tests:
        status = outcomes.get(node, "not-run") if node in index else "undefined"
        tests.append({"id": node, "browser": _browser(node), "status": status})
        if status == "failed":
            reasons.append(f"{node} failed")
    proof = [node for node in row.tests if not _browser(node)]
    if proof and not any(outcomes.get(node) == "passed" for node in proof):
        reasons.append("no mapped non-browser test passed in this run")
    return reasons, tests


def _marked(rows: list[JsonValue], key: str) -> list[JsonValue]:
    return [
        row["id"] for row in rows if isinstance(row, dict) and row.get(key) is not None
    ]


def _browser(node: str) -> bool:
    return node.startswith(BROWSER_PREFIX)


def run_cli(argv: list[str] | None = None) -> int:
    """Write a report; ``report`` exits 0 only when every mitigation is covered."""
    parser = argparse.ArgumentParser(prog="threat_model_coverage")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--output", required=True, type=Path)
    report.add_argument("--junit", action="append", type=Path, required=True)
    report.add_argument("--root", type=Path, default=PROJECT_ROOT)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--output", required=True, type=Path)
    inventory.add_argument("--root", type=Path, default=PROJECT_ROOT)
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    mitigations = load_mitigations(root)
    output: Path = arguments.output
    if arguments.command == "inventory":
        mapping = mapping_inventory(mitigations, defined_tests(root))
        output.write_bytes(canonical_bytes(mapping))
        missing = mapping["unmapped"]
        sys.stdout.write(
            f"threat-model mapping: {mapping['mapped']}/{mapping['mitigations']} "
            "mapped (not a coverage verdict)\n"
        )
        return 0 if isinstance(missing, list) and not missing else 1
    result = coverage_report(
        mitigations, defined_tests(root), executed_outcomes(arguments.junit)
    )
    output.write_bytes(canonical_bytes(result))
    counts = {
        state: len(value) if isinstance(value, list) else value
        for state, value in (
            ("partial", result["partial"]),
            ("gap", result["gaps"]),
            ("uncovered", result["uncovered"]),
        )
    }
    sys.stdout.write(
        f"threat-model coverage: {result['covered']}/{result['mitigations']} "
        f"covered, {counts['partial']} partial, {counts['gap']} gap, "
        f"{counts['uncovered']} uncovered\n"
    )
    return 0 if result["complete"] is True else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
