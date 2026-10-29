"""Live collection receipts are independent inputs to the aggregate proof."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from ops.testing import ci_pytest
from ops.testing import ci_pytest_artifacts as artifacts
from ops.testing.ci_pytest_collection import (
    validate_execution_collections,
    validate_execution_union,
    write_execution_collections,
)
from ops.testing.ci_pytest_partition import public_nodeid
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
)

from infra.ci_shard_support import (
    REFERENCE,
    rewrite_payload,
    write_json,
    write_python_artifacts,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    write_python_artifacts(root)
    return root


def test_live_and_executed_proof_matches_independent_fixture_input(
    evidence: Path,
) -> None:
    # Given: live input contains the complete fixture, independently of reports.
    expected = sorted(public_nodeid(nodeid) for nodeid in REFERENCE)
    digest = hashlib.sha256(json.dumps(expected).encode()).hexdigest()
    # When: both shard artifacts are combined.
    _, proof = artifacts.load_version(evidence, "3.12")
    # Then: both observed input and actual execution bind that exact population.
    assert proof["live_collection_count"] == proof["executed_count"] == len(REFERENCE)
    assert proof["live_collection_sha256"] == proof["executed_sha256"] == digest
    assert proof["union_equals_live_collection"] is True


@pytest.mark.parametrize("phase", ["parallel", "serial"])
@pytest.mark.parametrize(
    "case",
    ["added", "missing", "duplicate", "missing-worker", "unknown-worker"],
)
def test_live_drift_is_rejected_even_when_saved_reports_still_match(
    evidence: Path, phase: str, case: str
) -> None:
    # Given: reports and the saved reference remain unchanged and consistent.
    root = evidence / "renewal-python-py3.12-shard-1"

    def change(payload: JsonObject) -> None:
        observation = payload[phase]
        assert isinstance(observation, dict)
        processes = observation["processes"]
        assert isinstance(processes, dict)
        worker = next(iter(processes))
        nodes = observation["items"]
        assert isinstance(nodes, list)
        if case == "added":
            nodes.append(public_nodeid("tests/a.py::test_added_after_reference"))
        elif case == "missing":
            nodes.pop()
        elif case == "duplicate":
            repeated = [*nodes, nodes[0]]
            processes[worker] = {
                "count": len(repeated),
                "sha256": hashlib.sha256(
                    canonical_bytes(
                        list[JsonValue](sorted(str(node) for node in repeated))
                    )
                ).hexdigest(),
            }
        elif case == "missing-worker":
            del processes[worker]
        else:
            processes["unexpected-worker"] = processes.pop(worker)
        if case in {"added", "missing"}:
            for worker in processes:
                processes[worker] = {
                    "count": len(nodes),
                    "sha256": hashlib.sha256(
                        canonical_bytes(
                            list[JsonValue](sorted(str(node) for node in nodes))
                        )
                    ).hexdigest(),
                }

    # When: only the live receipt is changed and its payload hash rebound.
    rewrite_payload(root, "execution-collections.json", change)
    # Then: no reference/report-only proof can accept it.
    with pytest.raises(IsolationError):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("field", ["workers", "parallel", "serial"])
def test_every_execution_collection_field_is_required(
    evidence: Path, field: str
) -> None:
    def change(payload: JsonObject) -> None:
        del payload[field]

    rewrite_payload(
        evidence / "renewal-python-py3.12-shard-1",
        "execution-collections.json",
        change,
    )
    with pytest.raises(IsolationError):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("count", [0, 33, True, "4"])
def test_execution_collection_worker_count_is_closed(
    evidence: Path, count: int | bool | str
) -> None:
    def change(payload: JsonObject) -> None:
        payload["workers"] = count

    rewrite_payload(
        evidence / "renewal-python-py3.12-shard-1",
        "execution-collections.json",
        change,
    )
    with pytest.raises(IsolationError):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize(
    ("executed", "live"),
    [
        (["original"], ["original", "new-live-node"]),
        (["original", "unexpected"], ["original"]),
        (["original", "original"], ["original"]),
    ],
)
def test_actual_report_union_cannot_omit_repeat_or_invent_live_nodes(
    executed: list[str], live: list[str]
) -> None:
    with pytest.raises(IsolationError):
        validate_execution_union(executed, live)


def test_process_missing_a_node_cannot_hide_behind_other_complete_workers(
    evidence: Path,
) -> None:
    def change(payload: JsonObject) -> None:
        parallel = payload["parallel"]
        assert isinstance(parallel, dict)
        nodes = parallel["items"]
        processes = parallel["processes"]
        assert isinstance(nodes, list)
        assert isinstance(processes, dict)
        missing = nodes[:-1]
        processes["gw0"] = {
            "count": len(missing),
            "sha256": hashlib.sha256(canonical_bytes(missing)).hexdigest(),
        }

    rewrite_payload(
        evidence / "renewal-python-py3.12-shard-1",
        "execution-collections.json",
        change,
    )
    with pytest.raises(IsolationError):
        artifacts.load_version(evidence, "3.12")


def test_collection_capture_keeps_nodes_absent_from_saved_reference(
    tmp_path: Path,
) -> None:
    original = public_nodeid("tests/a.py::test_original")
    added = public_nodeid("tests/a.py::test_added")
    write_json(tmp_path / "collected.json", {"items": [original]})
    for worker, nodes in (
        ("gw0", [original]),
        ("gw1", [original, added]),
    ):
        write_json(
            tmp_path / f"parallel-{worker}-collection.json",
            {"items": list[JsonValue](nodes)},
        )
    write_json(tmp_path / "serial-main-collection.json", {"items": [original]})
    write_execution_collections(tmp_path, 2)
    payload, _ = load_json(tmp_path / "execution-collections.json")
    parallel = payload["parallel"]
    assert isinstance(parallel, dict)
    assert parallel["items"] == sorted((original, added))
    with pytest.raises(IsolationError):
        validate_execution_collections(
            payload, [original], {"parallel": [original], "serial": [original]}
        )


@pytest.mark.parametrize("drift", [False, True])
def test_local_driver_checks_live_input_before_matching_saved_reports(
    tmp_path: Path, drift: bool
) -> None:
    reference = [
        "tests/a.py::test_one[synthetic-private-value]",
        "tests/b.py::test_serial",
    ]
    observed = [public_nodeid(node) for node in reference]
    if drift:
        observed.append(public_nodeid("tests/a.py::test_added_after_reference"))
    for worker in range(2):
        write_json(
            tmp_path / f"parallel-gw{worker}-collection.json",
            {"items": list[JsonValue](observed)},
        )
    write_json(
        tmp_path / "serial-main-collection.json",
        {"items": [public_nodeid(reference[1])]},
    )
    write_execution_collections(tmp_path, 2)
    for phase, nodes in (("parallel", reference[:1]), ("serial", reference[1:])):
        reports: JsonObject = {
            node: [
                {"when": when, "outcome": "passed"}
                for when in ("setup", "call", "teardown")
            ]
            for node in nodes
        }
        write_json(tmp_path / f"{phase}-reports.json", reports)
    failures = ci_pytest.verify(tmp_path, reference, {reference[1]})
    if drift:
        assert failures
        assert "synthetic-private-value" not in "\n".join(failures)
    else:
        assert failures == []
