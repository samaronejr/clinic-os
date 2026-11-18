"""Deterministic, file-atomic partitions of the independent pytest reference.

``partition`` weights each test file by the committed duration table,
``ci-pytest-durations.json`` beside this module: recorded milliseconds per
file and phase, with a fixed per-test fallback for a file the table does not
know. ``DURATIONS_SHA256`` pins the table's bytes. Every shard, the driver and
the per-version gate load the same table: one that changed without its pin
fails closed, and so does a shard bundle that recorded another table's hash
(``ops.testing.ci_pytest_artifacts``). ``python -m ops.testing.ci_pytest_durations``
regenerates the table from one green run's shard bundles.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SHARDS: Final = (1, 2, 3, 4)
PYTHONS: Final = ("3.12", "3.13")
PHASES: Final = ("parallel", "serial")
ALGORITHM: Final = "file-duration-lpt-sha256-v2"
NODE_ID_ENCODING: Final = "file+sha256"
DURATIONS: Final = Path(__file__).with_name("ci-pytest-durations.json")
DURATIONS_SHA256: Final = (
    "3c68f24aa92076d40d9c50fe88451395dadb68715ed532a861330f41e8e8a5b1"
)
DURATIONS_SCHEMA: Final = 1
DURATION_SOURCES: Final = ("local", "hosted")
_FIELDS: Final = frozenset(
    {
        "fallback_ms_per_test",
        "files",
        "note",
        "revision_sha",
        "schema_version",
        "source",
    }
)
_TEST_FILE: Final = re.compile(r"tests/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py")


@dataclass(frozen=True, slots=True)
class DurationTable:
    """Recorded milliseconds per test file and phase, and the per-test fallback."""

    sha256: str
    source: str
    fallback_ms: int
    files: Mapping[str, Mapping[str, int]]

    def weight(self, file: str, phase: str, population: int) -> int:
        """Return the file's recorded milliseconds, or population x the fallback."""
        recorded = self.files.get(file, {}).get(phase)
        return population * self.fallback_ms if recorded is None else recorded


def _fail(message: str) -> Never:
    raise IsolationError(message)


def public_nodeid(nodeid: str) -> str:
    """Retain file locality and exact identity without publishing parameter values."""
    file = nodeid.split("::", 1)[0]
    return f"{file}::sha256-{hashlib.sha256(nodeid.encode()).hexdigest()}"


def encode_durations(
    *,
    source: str,
    note: str,
    revision_sha: str,
    fallback_ms: int,
    files: Mapping[str, Mapping[str, int]],
) -> bytes:
    """Return the table's one layout: sorted header fields, then a line per file."""
    header: dict[str, object] = {
        "fallback_ms_per_test": fallback_ms,
        "note": note,
        "revision_sha": revision_sha,
        "schema_version": DURATIONS_SCHEMA,
        "source": source,
    }
    lines = ["{"]
    lines.extend(
        f"  {json.dumps(key)}: {json.dumps(header[key], ensure_ascii=False)},"
        for key in sorted(header)
    )
    lines.append('  "files": {')
    rows = sorted(files.items())
    lines.extend(
        f"    {json.dumps(name)}: {json.dumps(dict(sorted(phases.items())))}"
        f"{',' if index < len(rows) - 1 else ''}"
        for index, (name, phases) in enumerate(rows)
    )
    lines.extend(("  }", "}"))
    return ("\n".join(lines) + "\n").encode()


def _milliseconds(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(f"duration table {label}: expected nonnegative whole milliseconds")
    return value


def _parsed_files(files: object) -> dict[str, dict[str, int]]:
    if not isinstance(files, dict) or not files:
        _fail("duration table lists no files")
    parsed: dict[str, dict[str, int]] = {}
    for name, phases in files.items():
        if not isinstance(name, str) or _TEST_FILE.fullmatch(name) is None:
            _fail(f"duration table names a non-test path: {name}")
        if not isinstance(phases, dict) or not phases or not set(phases) <= set(PHASES):
            _fail(f"duration table phases differ for {name}")
        parsed[name] = {
            str(phase): _milliseconds(spent, name) for phase, spent in phases.items()
        }
    return parsed


def parse_durations(raw: bytes, *, sha256: str) -> DurationTable:
    """Validate the table's closed schema and layout; anything else fails closed."""
    try:
        value: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"duration table is not JSON: {error}"
        raise IsolationError(message) from error
    if not isinstance(value, dict) or set(value) != _FIELDS:
        _fail("duration table fields differ")
    source, note, revision = value["source"], value["note"], value["revision_sha"]
    if value["schema_version"] != DURATIONS_SCHEMA or source not in DURATION_SOURCES:
        _fail("duration table schema version or source differs")
    if not isinstance(note, str) or not note or not isinstance(revision, str):
        _fail("duration table note or revision is malformed")
    fallback = _milliseconds(value["fallback_ms_per_test"], "fallback")
    if fallback == 0 or not revision:
        _fail("duration table fallback and revision must be set")
    parsed = _parsed_files(value["files"])
    canonical = encode_durations(
        source=str(source),
        note=note,
        revision_sha=revision,
        fallback_ms=fallback,
        files=parsed,
    )
    if canonical != raw:
        _fail("duration table is not in its canonical layout")
    return DurationTable(sha256, str(source), fallback, parsed)


def load_durations(
    path: Path = DURATIONS, *, pinned: str = DURATIONS_SHA256
) -> DurationTable:
    """Load the committed table; bytes that differ from the pin fail closed."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != pinned:
        _fail(
            f"duration table {path.name} drifted from its pinned SHA-256 "
            f"(found {digest}); regenerate it with ops.testing.ci_pytest_durations "
            "and update DURATIONS_SHA256"
        )
    return parse_durations(raw, sha256=digest)


def partition(
    reference: Sequence[str], serial: set[str], *, durations: DurationTable
) -> dict[int, dict[str, list[str]]]:
    """Balance each phase by recorded file duration, using SHA-256 to break ties.

    Heaviest files go first to the lightest shard. A file weighs its recorded
    milliseconds for the phase, or its test count times the table's fixed
    fallback when the table does not know it, so a table that knows no file
    balances by population. A file stays together within a phase, preserving
    loadfile scheduling and fixture locality. Neither filesystem order,
    Python's randomized hash nor this machine's timing is an input: the
    weights are the committed table's.
    """
    if not reference or len(set(reference)) != len(reference):
        message = "reference population is empty or repeats a node ID"
        raise IsolationError(message)
    if not serial <= set(reference):
        message = "serial population contains a non-reference node ID"
        raise IsolationError(message)
    result: dict[int, dict[str, list[str]]] = {
        shard: {phase: [] for phase in PHASES} for shard in SHARDS
    }
    for phase in PHASES:
        files: dict[str, list[str]] = defaultdict(list)
        for nodeid in reference:
            if (nodeid in serial) == (phase == "serial"):
                files[nodeid.split("::", 1)[0]].append(nodeid)
        weights = {
            name: durations.weight(name, phase, len(nodes))
            for name, nodes in files.items()
        }
        ordered = sorted(
            files,
            key=lambda name: (
                -weights[name],
                -len(files[name]),
                hashlib.sha256(name.encode()).digest(),
            ),
        )
        loads = dict.fromkeys(SHARDS, 0)
        for name in ordered:
            shard = min(SHARDS, key=lambda number: (loads[number], number))
            loads[shard] += weights[name]
            result[shard][phase].extend(sorted(files[name]))
    return result


def shard_weights(
    selections: Mapping[int, Mapping[str, Sequence[str]]],
    durations: DurationTable,
) -> dict[int, dict[str, int]]:
    """Return each shard's summed file weights per phase, as partition balanced."""
    return {
        shard: {
            phase: sum(
                durations.weight(name, phase, count)
                for name, count in Counter(
                    nodeid.split("::", 1)[0] for nodeid in phases[phase]
                ).items()
            )
            for phase in PHASES
        }
        for shard, phases in selections.items()
    }


def population_failures(
    expected: Mapping[str, Sequence[str]],
    reports: Mapping[str, Mapping[str, Sequence[Mapping[str, str]]]],
) -> list[str]:
    """Require each expected node to finish exactly once in its own phase."""
    failures: list[str] = []
    for phase in PHASES:
        counts = Counter(
            {
                nodeid: sum(entry.get("when") == "teardown" for entry in entries)
                for nodeid, entries in reports[phase].items()
            }
        )
        missing = set(expected[phase]) - counts.keys()
        missing.update(nodeid for nodeid in expected[phase] if counts[nodeid] == 0)
        extra = counts.keys() - set(expected[phase])
        duplicates = sorted(
            nodeid
            for nodeid, entries in reports[phase].items()
            if any(
                sum(entry.get("when") == when for entry in entries) > 1
                for when in ("setup", "call", "teardown")
            )
        )
        if missing:
            failures.append(
                f"{phase} phase did not run {len(missing)}: {sorted(missing)[:20]}"
            )
        if extra:
            failures.append(
                f"{phase} phase ran {len(extra)} unexpected: {sorted(extra)[:20]}"
            )
        if duplicates:
            failures.append(f"{phase} phase repeated node IDs: {duplicates[:20]}")
    if reports["parallel"].keys() & reports["serial"].keys():
        failures.append("a node ID ran in both phases")
    return failures
