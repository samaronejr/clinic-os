"""Partition, detector and raw coverage negatives for hosted-only sharding."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from coverage import CoverageData
from ops.testing import ci_pytest_artifacts as artifacts
from ops.testing.ci_pytest_partition import PHASES, SHARDS, partition, public_nodeid
from ops.testing.isolation_common import IsolationError, JsonObject

from infra.ci_shard_support import (
    EXITS,
    REFERENCE,
    SERIAL,
    rewrite_payload,
    write_json,
    write_python_artifacts,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_partition_is_order_independent_balanced_and_exactly_once() -> None:
    # Given: files occurring in both phases and unequal file populations.
    reference = [
        f"tests/{name}.py::test_{number}"
        for name, count in (("a", 12), ("b", 9), ("c", 5), ("d", 3), ("e", 2))
        for number in range(count)
    ]
    serial = {node for node in reference if node.endswith(("0", "1"))}
    # When: both input orderings are independently partitioned.
    selected = partition(reference, serial)
    # Then: each phase is balanced to at most its largest indivisible file.
    assert selected == partition(list(reversed(reference)), serial)
    flattened = [
        node
        for phases in selected.values()
        for nodes in phases.values()
        for node in nodes
    ]
    assert sorted(flattened) == sorted(reference)
    assert len(set(flattened)) == len(reference)
    for phase in PHASES:
        assert not set(selected[1][phase]) & set(selected[2][phase])
        largest = max(
            sum(
                node.startswith(f"tests/{name}.py::") for node in selected[shard][phase]
            )
            for name in "abcde"
            for shard in SHARDS
        )
        assert abs(len(selected[1][phase]) - len(selected[2][phase])) <= largest
        for name in "abcde":
            assert (
                sum(
                    any(
                        node.startswith(f"tests/{name}.py::")
                        for node in selected[shard][phase]
                    )
                    for shard in SHARDS
                )
                <= 1
            )


@pytest.mark.parametrize(
    ("reference", "serial"),
    [
        ([], set()),
        ([*REFERENCE, REFERENCE[0]], SERIAL),
        (REFERENCE, {"tests/no.py::test_no"}),
    ],
)
def test_reference_population_refuses_empty_duplicate_or_foreign_nodes(
    reference: list[str],
    serial: set[str],
) -> None:
    with pytest.raises(IsolationError):
        partition(reference, serial)


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    write_python_artifacts(root)
    return root


def test_complete_shards_prove_population_census_and_catalogs(evidence: Path) -> None:
    shards, proof = artifacts.load_version(evidence, "3.12")
    assert len(shards) == 2
    assert proof["reference_count"] == len(REFERENCE)
    assert proof["union_equals_reference"] is True
    assert proof["catalog_certificates"] == 2
    refusal = proof["refusal"]
    assert isinstance(refusal, dict)
    assert refusal["exits"] == refusal["seen"] == EXITS
    artifacts.validate_combined(evidence, "3.12", proof)


def test_published_node_ids_do_not_expose_parameter_values(evidence: Path) -> None:
    shard = evidence / "renewal-python-py3.12-shard-1"
    for path in shard.glob("*.json"):
        assert "synthetic-secret-should-not-publish" not in path.read_text()
    shards, _ = artifacts.load_version(evidence, "3.12")
    assert set(shards[0].reference) == {public_nodeid(nodeid) for nodeid in REFERENCE}
    assert public_nodeid(REFERENCE[0]) != public_nodeid(REFERENCE[1])


def test_public_identity_collision_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        artifacts, "public_nodeid", lambda nodeid: "tests/a.py::sha256-collision"
    )
    with pytest.raises(IsolationError, match="repeats an identity"):
        write_python_artifacts(tmp_path / "artifacts")


def test_reference_order_does_not_change_shard_population(evidence: Path) -> None:
    def change(collected: JsonObject) -> None:
        nodes = collected["items"]
        assert isinstance(nodes, list)
        nodes.reverse()

    root = evidence / "renewal-python-py3.12-shard-2"
    rewrite_payload(root, "collected.json", change)
    _, proof = artifacts.load_version(evidence, "3.12")
    assert proof["reference_count"] == len(REFERENCE)
    assert proof["union_equals_reference"] is True


@pytest.mark.parametrize("case", ["missing", "extra", "wrong-number", "wrong-python"])
def test_every_shard_identity_and_artifact_is_required(
    evidence: Path, case: str
) -> None:
    root = evidence / "renewal-python-py3.12-shard-2"
    if case == "missing":
        root.rename(root.with_name("absent"))
    elif case == "extra":
        root.with_name("renewal-python-py3.12-shard-3").mkdir()
    else:
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["shard" if case == "wrong-number" else "python_version"] = (
            1 if case == "wrong-number" else "3.13"
        )
        write_json(path, manifest)
    with pytest.raises(IsolationError):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("case", ["missing", "duplicate", "other-shard"])
@pytest.mark.parametrize("phase", PHASES)
def test_population_refuses_missing_repeated_and_misrouted_execution(
    evidence: Path,
    case: str,
    phase: str,
) -> None:
    root = evidence / "renewal-python-py3.12-shard-1"

    def change(reports: JsonObject) -> None:
        nodeid = next(iter(reports))
        if case == "missing":
            del reports[nodeid]
        elif case == "duplicate":
            entries = reports[nodeid]
            assert isinstance(entries, list)
            entries.append({"when": "teardown", "outcome": "passed"})
        else:
            other = public_nodeid(partition(REFERENCE, SERIAL)[2][phase][0])
            reports[other] = reports[nodeid]

    rewrite_payload(root, f"{phase}-reports.json", change)
    with pytest.raises(IsolationError, match=r"did not run|repeated node|unexpected"):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("case", ["missing", "duplicate", "different"])
def test_independent_reference_populations_must_agree(
    evidence: Path, case: str
) -> None:
    def change(collected: JsonObject) -> None:
        nodes = collected["items"]
        assert isinstance(nodes, list)
        if case == "missing":
            nodes.pop()
        elif case == "duplicate":
            nodes.append(nodes[0])
        else:
            nodes[0] = "tests/different.py::test_changed"

    rewrite_payload(
        evidence / "renewal-python-py3.12-shard-2", "collected.json", change
    )
    with pytest.raises(IsolationError):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize(
    "case",
    [
        "missing-exit",
        "unknown-exit",
        "missing-control",
        "errors",
        "violations",
        "missing-count",
        "double-count",
        "missing-gate",
        "duplicate-gate",
        "wrong-reference",
    ],
)
def test_refusal_census_fails_closed_after_hashes_are_rebound(
    evidence: Path,
    case: str,
) -> None:
    number = 1 if case == "missing-gate" else 2

    def change(receipt: JsonObject) -> None:
        stats = receipt["stats"]
        assert isinstance(stats, dict)
        if case == "missing-exit":
            receipt["seen"] = []
        elif case == "unknown-exit":
            receipt["seen"] = ["apps/unknown.py:1 (raise)"]
        elif case == "missing-control":
            receipt["control"] = False
        elif case == "errors":
            receipt["errors"] = ["synthetic integrity failure"]
        elif case == "violations":
            stats["violations"] = 1
        elif case == "missing-count":
            stats["evaluated_requests"] = 0
        elif case == "double-count":
            stats["responses"] = 2
        elif case in {"missing-gate", "duplicate-gate"}:
            receipt["gate"] = case == "duplicate-gate"
        else:
            receipt["exits"] = [EXITS[0]]

    rewrite_payload(
        evidence / f"renewal-python-py3.12-shard-{number}",
        "parallel-refusal.json",
        change,
    )
    with pytest.raises(IsolationError, match="refusal"):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("case", ["write", "reset", "unreadable", "failure"])
def test_catalog_certificates_cannot_be_averaged_or_dropped(
    evidence: Path,
    case: str,
) -> None:
    def change(certificate: JsonObject) -> None:
        after = certificate["after"]
        assert isinstance(after, dict)
        if case == "unreadable":
            certificate["before"] = None
        elif case == "failure":
            certificate["failures"] = ["synthetic connected backend"]
        elif case == "reset":
            after["server"] = ["on", "started", "loaded", "reset"]
        else:
            catalogs = after["catalogs"]
            assert isinstance(catalogs, dict)
            catalogs["pg_authid"] = {
                "inserted": 0,
                "updated": 1,
                "deleted": 0,
                "committed": 1,
                "analyzed": 1,
            }

    rewrite_payload(evidence / "renewal-python-py3.12-shard-1", "catalog.json", change)
    with pytest.raises(IsolationError, match="catalog"):
        artifacts.load_version(evidence, "3.12")


def test_payload_tampering_is_rejected_before_semantic_checks(evidence: Path) -> None:
    path = evidence / "renewal-python-py3.12-shard-1/parallel-refusal.json"
    write_json(path, {"control": True})
    with pytest.raises(IsolationError, match="hash differs"):
        artifacts.load_version(evidence, "3.12")


@pytest.mark.parametrize("missing", ["report.json", "coverage.xml"])
def test_combined_report_and_coverage_xml_are_required(
    evidence: Path, missing: str
) -> None:
    _, proof = artifacts.load_version(evidence, "3.12")
    (evidence / "renewal-coverage-py3.12" / missing).unlink()
    with pytest.raises(FileNotFoundError):
        artifacts.validate_combined(evidence, "3.12", proof)


@pytest.mark.parametrize("case", ["low-coverage", "wrong-shards", "wrong-xml"])
def test_combined_report_binds_both_shards_and_the_coverage_floor(
    evidence: Path,
    case: str,
) -> None:
    _, proof = artifacts.load_version(evidence, "3.12")
    path = evidence / "renewal-coverage-py3.12/report.json"
    report = json.loads(path.read_text())
    if case == "low-coverage":
        report["coverage_percent"] = 89.9
    elif case == "wrong-shards":
        report["reference_count"] = 1
    else:
        report["coverage_sha256"] = "0" * 64
    write_json(path, report)
    with pytest.raises(IsolationError):
        artifacts.validate_combined(evidence, "3.12", proof)


@pytest.mark.parametrize(
    ("lines", "passes"),
    [
        (({1, 2}, {3, 4}), True),
        (({1}, {1}), False),
    ],
)
@pytest.mark.parametrize("extra_source", [False, True])
def test_raw_coverage_is_unioned_before_enforcing_the_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: tuple[set[int], set[int]],
    passes: bool,
    extra_source: bool,
) -> None:
    # Given: neither shard alone reaches the floor, but complementary lines can.
    source = tmp_path / "apps/synthetic"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "example.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    if extra_source:
        (source / "untouched.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nomit = ["*/migrations/*"]\n'
    )
    targets = tmp_path / "coverage-targets.txt"
    targets.write_text("--cov=apps.synthetic\n")
    monkeypatch.setattr(artifacts, "REPOSITORY", tmp_path)
    monkeypatch.setattr(artifacts, "TARGETS", targets)
    shards = []
    for number, measured in enumerate(lines, 1):
        root = tmp_path / f"shard-{number}"
        root.mkdir()
        data = CoverageData(basename=str(root / "coverage.data"))
        remote = tmp_path / f"remote-runner-{number}"
        data.add_lines({str(remote / "apps/synthetic/example.py"): measured})
        data.write()
        shards.append(
            artifacts.ShardEvidence(
                root,
                number,
                remote,
                "a" * 40,
                "b" * 40,
                (),
                frozenset(),
                (),
                {},
                frozenset(),
                (),
            )
        )
    destination = tmp_path / "combined"
    destination.mkdir()
    # When / Then: coverage is measured from the union, never shard percentages.
    if passes and not extra_source:
        assert artifacts.combine_coverage(tuple(shards), destination) == 100
        assert 'line-rate="1"' in (destination / "coverage.xml").read_text()
    else:
        with pytest.raises(IsolationError, match="below --cov-fail-under=90"):
            artifacts.combine_coverage(tuple(shards), destination)
