"""Duration-weighted shards: the committed table, its pin and its refresh."""

from __future__ import annotations

import hashlib
import json
import math
from typing import TYPE_CHECKING

import pytest
from ops.testing import ci_pytest_artifacts as artifacts
from ops.testing import ci_pytest_durations as refresh
from ops.testing.ci_pytest_partition import (
    DURATIONS,
    DURATIONS_SHA256,
    PHASES,
    SHARDS,
    DurationTable,
    encode_durations,
    load_durations,
    parse_durations,
    partition,
    shard_weights,
)
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

from infra.ci_shard_support import rewrite_payload, write_json, write_python_artifacts

if TYPE_CHECKING:
    from pathlib import Path


def table(files: dict[str, dict[str, int]], fallback: int = 10) -> DurationTable:
    return DurationTable("0" * 64, "local", fallback, files)


def population(**counts: int) -> list[str]:
    return [
        f"tests/{name}.py::test_{number}"
        for name, count in counts.items()
        for number in range(count)
    ]


def heaviest(
    selections: dict[int, dict[str, list[str]]], durations: DurationTable
) -> int:
    loads = shard_weights(selections, durations)
    return max(load["parallel"] for load in loads.values())


def test_an_unknown_file_falls_back_to_its_population_times_the_fixed_weight() -> None:
    # Given: a table that knows one file, in one phase only.
    durations = table({"tests/known.py": {"parallel": 9_000}}, fallback=7)
    # Then: an unknown file, and the known file's other phase, weigh count x 7.
    assert durations.weight("tests/known.py", "parallel", 3) == 9_000
    assert durations.weight("tests/unknown.py", "parallel", 3) == 21
    assert durations.weight("tests/known.py", "serial", 2) == 14
    selections = partition(population(known=3, unknown=3), set(), durations=durations)
    loads = shard_weights(selections, durations)
    assert sorted(load["parallel"] for load in loads.values()) == [0, 0, 21, 9_000]


def test_recorded_durations_isolate_a_slow_small_file_that_counts_cannot() -> None:
    # Given: one slow two-test file and six fast, populous ones.
    counts = {"slow": 2} | {f"fast{number}": 40 for number in range(6)}
    reference = population(**counts)
    files = {"tests/slow.py": {"parallel": 600_000}} | {
        f"tests/fast{number}.py": {"parallel": 40_000} for number in range(6)
    }
    durations = table(files)
    # When: the reference is split by recorded duration, and by count alone.
    weighted = partition(reference, set(), durations=durations)
    counted = partition(reference, set(), durations=table({}))
    # Then: by duration the slow file runs alone; by count it shares a shard.
    alone = [
        shard
        for shard in SHARDS
        if any(
            node.startswith("tests/slow.py::") for node in weighted[shard]["parallel"]
        )
    ]
    assert len(alone) == 1
    assert {node.split("::")[0] for node in weighted[alone[0]]["parallel"]} == {
        "tests/slow.py"
    }
    assert heaviest(weighted, durations) == 600_000
    assert heaviest(counted, durations) > 600_000
    assert weighted == partition(list(reversed(reference)), set(), durations=durations)


def write_table(path: Path, *, fallback: int = 10) -> str:
    raw = encode_durations(
        source="local",
        note="synthetic",
        revision_sha="a" * 40,
        fallback_ms=fallback,
        files={"tests/a.py": {"parallel": 1, "serial": 2}},
    )
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_the_committed_table_loads_under_its_pin() -> None:
    durations = load_durations()
    assert durations.sha256 == DURATIONS_SHA256
    assert durations.sha256 == hashlib.sha256(DURATIONS.read_bytes()).hexdigest()
    assert durations.source in {"local", "hosted"}
    assert durations.files
    assert durations.fallback_ms > 0


def test_a_table_that_drifted_from_its_pin_fails_closed(tmp_path: Path) -> None:
    # Given: a table and the pin of its bytes.
    path = tmp_path / "durations.json"
    pinned = write_table(path)
    assert load_durations(path, pinned=pinned).fallback_ms == 10
    # When: one value changes but the pin does not.
    write_table(path, fallback=11)
    # Then: it fails closed instead of silently re-splitting the shards.
    with pytest.raises(IsolationError, match="drifted from its pinned SHA-256"):
        load_durations(path, pinned=pinned)


VALID_TABLE: JsonObject = {
    "fallback_ms_per_test": 10,
    "files": {"tests/a.py": {"parallel": 1}},
    "note": "synthetic",
    "revision_sha": "a" * 40,
    "schema_version": 1,
    "source": "local",
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("layout", "canonical layout"),
        ("fields", "fields differ"),
        ("source", "schema version or source"),
        ("fallback", "fallback and revision"),
        ("path", "non-test path"),
        ("phase", "phases differ"),
        ("negative", "nonnegative whole milliseconds"),
        ("fraction", "nonnegative whole milliseconds"),
    ],
)
def test_the_table_schema_and_layout_are_closed(change: str, message: str) -> None:
    payload = json.loads(json.dumps(VALID_TABLE))
    files = payload["files"]
    match change:
        case "fields":
            payload["extra"] = 1
        case "source":
            payload["source"] = "guessed"
        case "fallback":
            payload["fallback_ms_per_test"] = 0
        case "path":
            files["../outside.py"] = {"parallel": 1}
        case "phase":
            files["tests/a.py"] = {"collect": 1}
        case "negative":
            files["tests/a.py"] = {"parallel": -1}
        case "fraction":
            files["tests/a.py"] = {"parallel": 1.5}
    with pytest.raises(IsolationError, match=message):
        parse_durations(json.dumps(payload).encode(), sha256="0" * 64)


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    write_python_artifacts(root)
    return root


def test_the_gate_refuses_shards_partitioned_with_another_table(
    evidence: Path,
) -> None:
    # Given: shards bound to the committed table.
    _, proof = artifacts.load_version(evidence, "3.12")
    assert proof["durations_sha256"] == DURATIONS_SHA256
    # When / Then: a gate holding any other table refuses every one of them.
    other = DurationTable("f" * 64, "hosted", 10, {})
    with pytest.raises(IsolationError, match="invalid durations_sha256"):
        artifacts.load_version(evidence, "3.12", durations=other)


def test_a_shard_manifest_recording_another_table_is_refused(evidence: Path) -> None:
    path = evidence / "renewal-python-py3.12-shard-3/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["durations_sha256"] = "f" * 64
    write_json(path, manifest)
    with pytest.raises(IsolationError, match="shard 3: invalid durations_sha256"):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("duration", ["missing", -0.5, True, "0.1", math.inf, math.nan])
def test_every_report_carries_finite_nonnegative_seconds(
    evidence: Path, duration: JsonValue
) -> None:
    def change(reports: JsonObject) -> None:
        entries = next(iter(reports.values()))
        assert isinstance(entries, list)
        entry = entries[0]
        assert isinstance(entry, dict)
        if duration == "missing":
            del entry["duration"]
        else:
            entry["duration"] = duration

    rewrite_payload(
        evidence / "renewal-python-py3.12-shard-1", "parallel-reports.json", change
    )
    with pytest.raises(IsolationError, match=r"duration|fields differ"):
        artifacts.load_version(evidence, "3.12")


def bundle(
    root: Path,
    name: str,
    phases: dict[str, dict[str, tuple[float, float, float]]],
    *,
    revision: str = "a" * 40,
    exits: JsonObject | None = None,
) -> Path:
    path = root / name
    path.mkdir()
    write_json(
        path / "manifest.json",
        {
            "exits": exits or {"parallel": 0, "serial": 0},
            "failures": [],
            "revision_sha": revision,
        },
    )
    for phase in PHASES:
        reports: JsonObject = {
            nodeid: [
                {"when": when, "outcome": "passed", "duration": spent}
                for when, spent in zip(
                    ("setup", "call", "teardown"), seconds, strict=True
                )
            ]
            for nodeid, seconds in phases.get(phase, {}).items()
        }
        write_json(path / f"{phase}-reports.json", reports)
    return path


def test_refresh_sums_files_per_phase_with_the_median_test_as_fallback(
    tmp_path: Path,
) -> None:
    # Given: one run's two shard bundles, node IDs in their public encoding.
    one = bundle(
        tmp_path,
        "shard-1",
        {
            "parallel": {
                "tests/a.py::sha256-1": (0.1, 1.0, 0.1),
                "tests/a.py::sha256-2": (0.0, 0.5, 0.0),
            },
            "serial": {"tests/b.py::sha256-3": (0.0, 2.0, 0.0)},
        },
    )
    two = bundle(
        tmp_path, "shard-2", {"parallel": {"tests/c.py::sha256-4": (0, 0.25, 0)}}
    )
    output = tmp_path / "durations.json"
    arguments = [
        *("--source", "local", "--note", "synthetic", "--output", str(output)),
        *(str(one), str(two)),
    ]
    # When: the table is regenerated twice.
    assert refresh.main(arguments) == 0
    first = output.read_bytes()
    assert refresh.main(arguments) == 0
    # Then: the bytes repeat, files sum per phase, the median test is the fallback.
    assert output.read_bytes() == first
    durations = parse_durations(first, sha256=hashlib.sha256(first).hexdigest())
    assert dict(durations.files) == {
        "tests/a.py": {"parallel": 1_700},
        "tests/b.py": {"serial": 2_000},
        "tests/c.py": {"parallel": 250},
    }
    assert durations.fallback_ms == 850


@pytest.mark.parametrize("case", ["failed", "revisions", "twice"])
def test_refresh_refuses_a_failed_mixed_or_doubled_measurement(
    tmp_path: Path, case: str
) -> None:
    measured: dict[str, dict[str, tuple[float, float, float]]] = {
        "parallel": {"tests/a.py::sha256-1": (0, 1.0, 0)}
    }
    other: dict[str, dict[str, tuple[float, float, float]]] = {
        "parallel": {"tests/b.py::sha256-2": (0, 1.0, 0)}
    }
    one = bundle(
        tmp_path,
        "shard-1",
        measured,
        exits={"parallel": 1, "serial": 0} if case == "failed" else None,
    )
    two = bundle(
        tmp_path,
        "shard-2",
        measured if case == "twice" else other,
        revision="b" * 40 if case == "revisions" else "a" * 40,
    )
    with pytest.raises(
        IsolationError, match=r"not a green|different revisions|twice|two"
    ):
        refresh.measure([one, two])
    output = tmp_path / "durations.json"
    arguments = ["--source", "local", "--note", "x", "--output", str(output)]
    assert refresh.main([*arguments, str(one), str(two)]) == 2
    assert not output.exists()
