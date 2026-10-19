"""Deterministic, file-atomic partitions of the independent pytest reference."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SHARDS: Final = (1, 2)
PYTHONS: Final = ("3.12", "3.13")
PHASES: Final = ("parallel", "serial")
ALGORITHM: Final = "file-population-lpt-sha256-v1"
NODE_ID_ENCODING: Final = "file+sha256"


def public_nodeid(nodeid: str) -> str:
    """Retain file locality and exact identity without publishing parameter values."""
    file = nodeid.split("::", 1)[0]
    return f"{file}::sha256-{hashlib.sha256(nodeid.encode()).hexdigest()}"


def partition(
    reference: Sequence[str], serial: set[str]
) -> dict[int, dict[str, list[str]]]:
    """Balance each phase by file population, using SHA-256 to break ties.

    Largest files go first to the least populated shard. A file stays together
    within a phase, preserving loadfile scheduling and fixture locality. Neither
    filesystem order, Python's randomized hash nor machine timing is an input.
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
        ordered = sorted(
            files,
            key=lambda name: (
                -len(files[name]),
                hashlib.sha256(name.encode()).digest(),
            ),
        )
        for name in ordered:
            shard = min(SHARDS, key=lambda number: (len(result[number][phase]), number))
            result[shard][phase].extend(sorted(files[name]))
    return result


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
