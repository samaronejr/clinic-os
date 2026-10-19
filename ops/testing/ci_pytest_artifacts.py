"""Fail-closed hosted shard evidence and per-Python coverage combination."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException

from ops.testing.ci_pytest_collection import (
    validate_execution_collections,
    validate_execution_union,
)
from ops.testing.ci_pytest_partition import (
    ALGORITHM,
    NODE_ID_ENCODING,
    PHASES,
    PYTHONS,
    SHARDS,
    partition,
    population_failures,
    public_nodeid,
)
from ops.testing.ci_pytest_plugin import CatalogCounters, Counters, catalog_writes
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Never

REPOSITORY: Final = Path(__file__).resolve().parents[2]
TARGETS: Final = REPOSITORY / "ops/testing/coverage-targets.txt"
COVERAGE_MINIMUM: Final = 90
FULL_PERCENT: Final = 100
PERCENT_TOLERANCE: Final = 1e-8
SERVER_FIELD_COUNT: Final = 4
FILES: Final = (
    "collected.json",
    "execution-collections.json",
    "parallel-reports.json",
    "serial-reports.json",
    "catalog.json",
    "collect-refusal.json",
    "parallel-refusal.json",
    "serial-refusal.json",
    "coverage.data",
)
STATS: Final = frozenset(
    {
        "client_requests",
        "evaluated_requests",
        "responses",
        "refusals",
        "violations",
        "elapsed_ns",
        "integrity_ns",
        "trace_ns",
    }
)


@dataclass(frozen=True, slots=True)
class ShardEvidence:
    """A validated shard, retaining raw coverage and bound payload hashes."""

    root: Path
    shard: int
    repository: Path
    revision: str
    tree: str
    reference: tuple[str, ...]
    serial: frozenset[str]
    receipts: tuple[JsonObject, ...]
    hashes: JsonObject
    live: frozenset[str]
    executed: tuple[str, ...]


def _fail(message: str) -> Never:
    raise IsolationError(message)


def _object(value: JsonValue, label: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{label}: expected an object")
    return value


def _strings(value: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{label}: expected a string list")
    return tuple(str(item) for item in value)


def _string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label}: expected a nonempty string")
    return value


def _integer(value: JsonValue, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(f"{label}: expected a nonnegative integer")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: JsonObject) -> None:
    with path.open("xb") as handle:
        handle.write(canonical_bytes(payload))
    path.chmod(0o400)


def source_identity(repository: Path) -> tuple[str, str]:
    """Bind receipts to the checked-out commit and tree, never an env claim."""
    result = subprocess.run(  # noqa: S603 - fixed git query, no shell
        ["git", "-C", str(repository), "rev-parse", "HEAD", "HEAD^{tree}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    revision, tree = result.stdout.splitlines()
    return revision, tree


def publish(
    work: Path,
    destination: Path,
    *,
    shard: int,
    exits: dict[str, int],
    failures: list[str],
) -> None:
    """Retain immutable, hash-bound evidence outside the disposable work root."""
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    hashes: JsonObject = {}
    for name in FILES:
        if name.endswith(".json"):
            value: JsonValue = json.loads((work / name).read_text("utf-8"))
            payload = _object(value, name)
            if name == "collected.json":
                original = _strings(payload["items"], "reference node IDs")
                encoded = [public_nodeid(nodeid) for nodeid in original]
                if len(set(encoded)) != len(original):
                    _fail("public node ID encoding repeats an identity")
                payload["items"] = list[JsonValue](encoded)
                payload["manifest"] = {
                    public_nodeid(prefix): [
                        public_nodeid(nodeid) for nodeid in _strings(nodes, prefix)
                    ]
                    for prefix, nodes in _object(
                        payload["manifest"], "manifest"
                    ).items()
                }
            elif name.endswith("-reports.json"):
                payload = {
                    public_nodeid(nodeid): entries
                    for nodeid, entries in payload.items()
                }
            _write(destination / name, payload)
        else:
            shutil.copyfile(work / name, destination / name)
        (destination / name).chmod(0o400)
        hashes[name] = _digest(destination / name)
    revision, tree = source_identity(REPOSITORY)
    manifest: JsonObject = {
        "schema_version": 2,
        "algorithm": ALGORITHM,
        "node_id_encoding": NODE_ID_ENCODING,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "shard": shard,
        "shards": len(SHARDS),
        "repository": str(REPOSITORY),
        "revision_sha": revision,
        "tree_sha": tree,
        "coverage_targets_sha256": _digest(TARGETS),
        "exits": dict(exits),
        "failures": ["shard verification failed"] if failures else [],
        "files": hashes,
    }
    _write(destination / "manifest.json", manifest)


def _reports(path: Path) -> dict[str, list[dict[str, str]]]:
    raw, _ = load_json(path)
    reports: dict[str, list[dict[str, str]]] = {}
    for nodeid, entries in raw.items():
        if not isinstance(entries, list):
            _fail(f"{path.name}: malformed reports")
        parsed: list[dict[str, str]] = []
        for value in entries:
            entry = _object(value, "test report")
            if set(entry) != {"when", "outcome"}:
                _fail("test report fields differ")
            when = _string(entry["when"], "report phase")
            outcome = _string(entry["outcome"], "report outcome")
            if when not in {"setup", "call", "teardown"} or outcome not in {
                "passed",
                "skipped",
            }:
                _fail("test report is failed or malformed")
            parsed.append({"when": when, "outcome": outcome})
        reports[nodeid] = parsed
    return reports


def _reading(value: JsonValue) -> Counters:
    record = _object(value, "catalog reading")
    server = _strings(record.get("server"), "catalog server")
    if len(server) != SERVER_FIELD_COUNT:
        _fail("catalog server identity is incomplete")
    catalogs: dict[str, CatalogCounters] = {}
    for name, raw in _object(record.get("catalogs"), "catalog counters").items():
        counts = _object(raw, "catalog tuple counters")
        if set(counts) != {"inserted", "updated", "deleted", "committed", "analyzed"}:
            _fail("catalog tuple counters are incomplete")
        catalogs[name] = CatalogCounters(
            *(
                _integer(counts[field], field)
                for field in (
                    "inserted",
                    "updated",
                    "deleted",
                    "committed",
                    "analyzed",
                )
            )
        )
    return Counters(server, catalogs)


def _catalog_certificate(path: Path) -> None:
    certificate, _ = load_json(path)
    if (
        set(certificate) != {"before", "after", "failures"}
        or certificate["failures"] != []
    ):
        _fail("shared-catalog certificate is incomplete or failed")
    writes, problems = catalog_writes(
        _reading(certificate["before"]), _reading(certificate["after"])
    )
    if writes or problems:
        _fail(f"shared-catalog certificate cannot certify shard: {writes} {problems}")


def _receipt(path: Path) -> JsonObject:
    receipt, _ = load_json(path)
    if set(receipt) != {"control", "errors", "exits", "gate", "seen", "stats"}:
        _fail("refusal census receipt is incomplete")
    if receipt["control"] is not True or receipt["errors"] != []:
        _fail("refusal census has errors or no positive control")
    if not isinstance(receipt["gate"], bool):
        _fail("refusal census gate selection is malformed")
    stats = _object(receipt["stats"], "refusal statistics")
    if set(stats) != STATS:
        _fail("refusal statistics are incomplete")
    counts = {name: _integer(value, name) for name, value in stats.items()}
    if (
        counts["violations"]
        or not (
            counts["client_requests"]
            == counts["evaluated_requests"]
            == counts["responses"]
        )
        or counts["refusals"] > counts["responses"]
    ):
        _fail("refusal census counts do not certify exactly-once observation")
    for field in ("seen", "exits"):
        values = _strings(receipt[field], f"refusal {field}")
        if len(set(values)) != len(values):
            _fail(f"refusal {field} repeats a location")
    return receipt


def _load_shard(root: Path, version: str, number: int) -> ShardEvidence:
    manifest, _ = load_json(root / "manifest.json")
    expected: JsonObject = {
        "schema_version": 2,
        "algorithm": ALGORITHM,
        "node_id_encoding": NODE_ID_ENCODING,
        "python_version": version,
        "shard": number,
        "shards": len(SHARDS),
        "coverage_targets_sha256": _digest(TARGETS),
        "exits": {"parallel": 0, "serial": 0},
        "failures": [],
    }
    for key, value in expected.items():
        if canonical_bytes(manifest.get(key)) != canonical_bytes(value):
            _fail(f"Python {version} shard {number}: invalid {key}")
    if set(manifest) != set(expected) | {
        "repository",
        "revision_sha",
        "tree_sha",
        "files",
    }:
        _fail("shard manifest fields differ")
    hashes = _object(manifest["files"], "shard file hashes")
    if set(hashes) != set(FILES):
        _fail("shard payload file set is incomplete")
    for name in FILES:
        if hashes[name] != _digest(root / name):
            _fail(f"shard payload hash differs: {name}")
    collected, _ = load_json(root / "collected.json")
    reference = _strings(collected.get("items"), "reference population")
    manifest_matches = _object(collected.get("manifest"), "serial classification")
    serial: set[str] = set()
    for nodes in manifest_matches.values():
        matches = _strings(nodes, "serial classification matches")
        if not matches:
            _fail("serial classification matches no test")
        serial.update(matches)
    expected_population = partition(reference, serial)[number]
    collections, _ = load_json(root / "execution-collections.json")
    live = validate_execution_collections(collections, reference, expected_population)
    reports = {phase: _reports(root / f"{phase}-reports.json") for phase in PHASES}
    failures = population_failures(expected_population, reports)
    if failures:
        _fail("; ".join(failures))
    _catalog_certificate(root / "catalog.json")
    return ShardEvidence(
        root,
        number,
        Path(_string(manifest["repository"], "repository")),
        _string(manifest["revision_sha"], "revision"),
        _string(manifest["tree_sha"], "tree"),
        reference,
        frozenset(serial),
        tuple(
            _receipt(root / f"{phase}-refusal.json") for phase in ("collect", *PHASES)
        ),
        hashes,
        live,
        tuple(nodeid for phase in PHASES for nodeid in reports[phase]),
    )


def _census(shards: tuple[ShardEvidence, ...]) -> JsonObject:
    required = set(_strings(shards[0].receipts[0]["exits"], "required refusal exits"))
    if not required:
        _fail("reference collection derived no refusal exits")
    seen: set[str] = set()
    gates = 0
    stats = dict.fromkeys(sorted(STATS), 0)
    for shard in shards:
        if shard.receipts[0]["gate"] is not True:
            _fail("reference collection omitted the refusal exit oracle")
        for receipt in shard.receipts:
            if set(_strings(receipt["exits"], "refusal exits")) != required:
                _fail("refusal receipts disagree on the derived reference exits")
        for receipt in shard.receipts[1:]:
            gates += receipt["gate"] is True
            seen.update(_strings(receipt["seen"], "observed refusal exits"))
            for name, value in _object(receipt["stats"], "refusal statistics").items():
                stats[name] += _integer(value, name)
    if gates != 1:
        _fail("refusal exit oracle must run in exactly one phase and shard")
    if seen != required:
        _fail(
            f"refusal exit union differs: missing={sorted(required - seen)} "
            f"extra={sorted(seen - required)}"
        )
    return {
        "exits": list[JsonValue](sorted(required)),
        "seen": list[JsonValue](sorted(seen)),
        "stats": dict(stats),
    }


def load_version(
    artifacts: Path,
    version: str,
) -> tuple[tuple[ShardEvidence, ...], JsonObject]:
    """Prove the exact reference population and every detector across two shards."""
    expected = {f"renewal-python-py{version}-shard-{number}" for number in SHARDS}
    found = {
        path.name for path in artifacts.glob(f"renewal-python-py{version}-shard-*")
    }
    if found != expected:
        _fail(
            f"Python {version}: missing or unexpected shard artifacts: "
            f"{sorted(found ^ expected)}"
        )
    shards = tuple(
        _load_shard(
            artifacts / f"renewal-python-py{version}-shard-{number}", version, number
        )
        for number in SHARDS
    )
    first = shards[0]
    for shard in shards[1:]:
        if (shard.revision, shard.tree, frozenset(shard.reference), shard.serial) != (
            first.revision,
            first.tree,
            frozenset(first.reference),
            first.serial,
        ):
            _fail("shards disagree on source identity or reference population")
    selections = partition(first.reference, set(first.serial))
    executed = [nodeid for shard in shards for nodeid in shard.executed]
    live = {nodeid for shard in shards for nodeid in shard.live}
    validate_execution_union(executed, live)
    proof: JsonObject = {
        "schema_version": 2,
        "algorithm": ALGORITHM,
        "node_id_encoding": NODE_ID_ENCODING,
        "python_version": version,
        "revision_sha": first.revision,
        "tree_sha": first.tree,
        "coverage_targets_sha256": _digest(TARGETS),
        "reference_count": len(first.reference),
        "reference_sha256": hashlib.sha256(
            json.dumps(sorted(first.reference)).encode()
        ).hexdigest(),
        "live_collection_count": len(live),
        "live_collection_sha256": hashlib.sha256(
            json.dumps(sorted(live)).encode()
        ).hexdigest(),
        "executed_count": len(executed),
        "executed_sha256": hashlib.sha256(
            json.dumps(sorted(executed)).encode()
        ).hexdigest(),
        "shards": {
            str(shard.shard): {
                "files": shard.hashes,
                "counts": {
                    phase: len(selections[shard.shard][phase]) for phase in PHASES
                },
            }
            for shard in shards
        },
        "refusal": _census(shards),
        "catalog_certificates": len(shards),
        "union_equals_live_collection": True,
        "union_equals_reference": True,
    }
    return shards, proof


def combine_coverage(shards: tuple[ShardEvidence, ...], destination: Path) -> float:
    """Union raw line data, remap source roots, and enforce the unchanged scope."""
    targets = [
        line.removeprefix("--cov=")
        for line in TARGETS.read_text("utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    coverage = Coverage(
        data_file=str(destination / "coverage.data"),
        config_file=str(REPOSITORY / "pyproject.toml"),
        source=[str(REPOSITORY / target.replace(".", "/")) for target in targets],
    )
    data = coverage.get_data()
    omits = coverage.get_option("run:omit")
    if not isinstance(omits, list) or not all(
        isinstance(pattern, str) for pattern in omits
    ):
        _fail("coverage run omissions are not a string list")
    for shard in shards:
        raw = CoverageData(basename=str(shard.root / "coverage.data"))
        raw.read()
        if not raw.measured_files() or raw.has_arcs():
            _fail(
                "shard line coverage data is empty or has a different measurement mode"
            )

        def remap(filename: str, repository: Path = shard.repository) -> str:
            path = Path(filename)
            relative = path.relative_to(repository) if path.is_absolute() else path
            if ".." in relative.parts:
                _fail("coverage source path escapes its repository")
            if not any(
                relative.is_relative_to(target.replace(".", "/")) for target in targets
            ):
                _fail("coverage source is outside coverage-targets.txt")
            return str(REPOSITORY / relative)

        data.update(raw, map_path=remap)
    # Reporting a union must still count source files that no shard imported.
    # CoverageData.update alone only knows the files named by its inputs.
    data.touch_files(
        [
            str(path)
            for target in targets
            for path in (REPOSITORY / target.replace(".", "/")).rglob("*.py")
            if not any(fnmatch.fnmatch(str(path), pattern) for pattern in omits)
        ]
    )
    coverage.save()
    percent = coverage.report(show_missing=True)
    coverage.xml_report(outfile=str(destination / "coverage.xml"))
    if percent < COVERAGE_MINIMUM:
        _fail(f"combined coverage {percent:.6f}% is below --cov-fail-under=90")
    return percent


def validate_combined(artifacts: Path, version: str, proof: JsonObject) -> None:
    """RC rechecks shard proof bindings and the combined, hash-bound coverage XML."""
    root = artifacts / f"renewal-coverage-py{version}"
    report, _ = load_json(root / "report.json")
    if set(report) != set(proof) | {"coverage_percent", "coverage_sha256"}:
        _fail("combined Python report fields differ")
    if any(report[key] != value for key, value in proof.items()):
        _fail("combined Python report does not bind the validated shards")
    xml = root / "coverage.xml"
    if report["coverage_sha256"] != _digest(xml):
        _fail("combined coverage XML hash differs")
    document = ET.parse(xml).getroot()  # noqa: S314 - hash-bound runner artifact
    valid = int(document.attrib["lines-valid"])
    covered = int(document.attrib["lines-covered"])
    percent = report["coverage_percent"]
    if (
        valid <= 0
        or not 0 <= covered <= valid
        or not isinstance(percent, (float, int))
        or isinstance(percent, bool)
        or not math.isfinite(percent)
        or percent > FULL_PERCENT
        or percent < COVERAGE_MINIMUM
        or abs(percent - 100 * covered / valid) > PERCENT_TOLERANCE
    ):
        _fail("combined coverage does not meet the unchanged ninety percent floor")


def main(argv: Sequence[str] | None = None) -> int:
    """Combine one Python version's complete hosted shard artifacts."""
    parser = argparse.ArgumentParser(prog="python -m ops.testing.ci_pytest_artifacts")
    parser.add_argument("--python-version", choices=PYTHONS, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        shards, proof = load_version(arguments.artifacts, arguments.python_version)
        if source_identity(REPOSITORY) != (shards[0].revision, shards[0].tree):
            _fail("shards did not test the checked-out source revision")
        arguments.output.mkdir(parents=True, mode=0o700, exist_ok=False)
        percent = combine_coverage(shards, arguments.output)
        proof["coverage_percent"] = percent
        proof["coverage_sha256"] = _digest(arguments.output / "coverage.xml")
        _write(arguments.output / "report.json", proof)
    except (IsolationError, OSError, ValueError, CoverageException) as error:
        sys.stderr.write(f"ci-pytest-artifacts: {error}\n")
        return 2
    sys.stdout.write(
        f"ci-pytest-artifacts: Python {arguments.python_version} "
        f"reference={proof['reference_count']} shards=2 checks=passed "
        f"combined_coverage={percent:.6f}%\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
