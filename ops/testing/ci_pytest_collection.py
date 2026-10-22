"""Live execution-collection observations for the CI population proof."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from ops.testing.ci_pytest_partition import PHASES
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path
    from typing import Never

MAX_WORKERS: Final = 32


def _fail(message: str) -> Never:
    raise IsolationError(message)


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("execution collection must be an object")
    return value


def _nodes(value: JsonValue) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(node, str) for node in value):
        _fail("execution collection node IDs must be strings")
    return tuple(str(node) for node in value)


def _signature(nodes: Collection[str]) -> JsonObject:
    return {
        "count": len(nodes),
        "sha256": hashlib.sha256(
            canonical_bytes(list[JsonValue](sorted(nodes)))
        ).hexdigest(),
    }


def serial_collection_scope(
    reference: Sequence[str], selected_serial: Sequence[str]
) -> set[str]:
    """Include every reference item in the serial invocation's selected files."""
    files = {nodeid.split("::", 1)[0] for nodeid in selected_serial}
    return {nodeid for nodeid in reference if nodeid.split("::", 1)[0] in files}


def write_execution_collections(work: Path, workers: int) -> None:
    """Retain full live unions and every process's input count and fingerprint."""
    payload: JsonObject = {"workers": workers}
    for phase in PHASES:
        processes: JsonObject = {}
        live: set[str] = set()
        for path in sorted(work.glob(f"{phase}-*-collection.json")):
            raw, _ = load_json(path)
            if set(raw) != {"items"}:
                _fail("execution process collection fields differ")
            worker = path.name.removeprefix(f"{phase}-").removesuffix(
                "-collection.json"
            )
            nodes = _nodes(raw["items"])
            processes[worker] = _signature(nodes)
            live.update(nodes)
        payload[phase] = {
            "items": list[JsonValue](sorted(live)),
            "processes": processes,
        }
    path = work / "execution-collections.json"
    with path.open("xb") as handle:
        _ = handle.write(canonical_bytes(payload))
    path.chmod(0o400)


def validate_execution_collections(
    payload: JsonObject,
    reference: Sequence[str],
    selection: Mapping[str, Sequence[str]],
) -> frozenset[str]:
    """Validate all live worker inputs, preserving restricted serial scope."""
    if set(payload) != {"workers", *PHASES}:
        _fail("execution collection fields differ")
    count = payload["workers"]
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= MAX_WORKERS
    ):
        _fail("execution collection worker count is invalid")
    live: set[str] = set()
    for phase in PHASES:
        observation = _object(payload[phase])
        if set(observation) != {"items", "processes"}:
            _fail("execution phase collection fields differ")
        processes = _object(observation["processes"])
        workers = (
            {f"gw{number}" for number in range(count)}
            if phase == "parallel"
            else {"main"}
        )
        if set(processes) != workers:
            _fail(f"{phase} execution collection workers differ")
        scope = (
            set(reference)
            if phase == "parallel"
            else serial_collection_scope(reference, selection["serial"])
        )
        nodes = _nodes(observation["items"])
        observed = set(nodes)
        if len(nodes) != len(observed) or observed != scope:
            _fail(
                f"{phase} execution collection differs from reference scope: "
                f"missing={len(scope - observed)} "
                f"unexpected={len(observed - scope)} "
                f"duplicates={len(nodes) - len(observed)}"
            )
        expected = canonical_bytes(_signature(scope))
        for value in processes.values():
            if canonical_bytes(_object(value)) != expected:
                _fail(f"{phase} execution process collection differs from scope")
        live.update(nodes)
    return frozenset(live)


def validate_execution_union(executed: Sequence[str], live: Collection[str]) -> None:
    """Require actual reports to cover the full observed population once."""
    if len(executed) != len(set(executed)) or set(executed) != set(live):
        _fail("executed union differs from full live collection")
