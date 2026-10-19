"""Synthetic, hash-bound hosted Python artifacts for the CI contract tests."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import TYPE_CHECKING

from coverage import CoverageData
from ops.testing import ci_pytest_artifacts as artifacts
from ops.testing.ci_pytest_partition import PHASES, PYTHONS, SHARDS, partition
from ops.testing.ci_pytest_plugin import COUNTED_SHARED, CatalogCounters, Counters
from ops.testing.isolation_common import JsonObject, canonical_bytes, load_json

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

REFERENCE = (
    "tests/a.py::test_one",
    "tests/a.py::test_two[synthetic-secret-should-not-publish]",
    "tests/b.py::test_three",
    "tests/b.py::test_four",
    "tests/c.py::test_serial",
    "tests/d.py::test_serial",
)
SERIAL = set(REFERENCE[-2:])
EXITS = ["apps/core/workspace.py:1 (raise)", "apps/core/workspace.py:2 (raise)"]


def write_json(path: Path, value: JsonObject) -> None:
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(canonical_bytes(value))
    path.chmod(0o400)


def receipt(*, seen: list[str], gate: bool) -> JsonObject:
    stats: JsonObject = dict.fromkeys(sorted(artifacts.STATS), 0)
    for name in ("client_requests", "evaluated_requests", "responses", "refusals"):
        stats[name] = len(seen)
    return {
        "control": True,
        "errors": [],
        "gate": gate,
        "seen": [*seen],
        "exits": [*EXITS],
        "stats": stats,
    }


def write_python_artifacts(
    root: Path,
    *,
    revision: str = "a" * 40,
    tree: str = "b" * 40,
) -> None:
    """Publish both versions' shards and independently validated merge reports."""
    root.mkdir(parents=True, exist_ok=True)
    selection = partition(REFERENCE, SERIAL)
    reading = asdict(
        Counters(
            ("on", "started", "loaded", "never"),
            {name: CatalogCounters(0, 0, 0, 0, 1) for name in COUNTED_SHARED},
        )
    )
    for version in PYTHONS:
        for number in SHARDS:
            work = root / f"work-{version}-{number}"
            work.mkdir()
            collected: JsonObject = {
                "items": [*REFERENCE],
                "manifest": {
                    "tests/c.py": [REFERENCE[-2]],
                    "tests/d.py": [REFERENCE[-1]],
                },
            }
            write_json(work / "collected.json", collected)
            for phase in PHASES:
                reports: JsonObject = {
                    nodeid: [
                        {"when": when, "outcome": "passed"}
                        for when in ("setup", "call", "teardown")
                    ]
                    for nodeid in selection[number][phase]
                }
                write_json(work / f"{phase}-reports.json", reports)
            write_json(
                work / "catalog.json",
                {
                    "before": reading,
                    "after": reading,
                    "failures": [],
                },
            )
            write_json(work / "collect-refusal.json", receipt(seen=[], gate=True))
            write_json(
                work / "parallel-refusal.json",
                receipt(
                    seen=[EXITS[number - 1]],
                    gate=number == 1,
                ),
            )
            write_json(work / "serial-refusal.json", receipt(seen=[], gate=False))
            data = CoverageData(basename=str(work / "coverage.data"))
            data.add_lines({str(artifacts.REPOSITORY / "apps/audit/__init__.py"): {1}})
            data.write()
            target = root / f"renewal-python-py{version}-shard-{number}"
            artifacts.publish(
                work,
                target,
                shard=number,
                exits={"parallel": 0, "serial": 0},
                failures=[],
            )
            manifest, _ = load_json(target / "manifest.json")
            manifest.update(
                {
                    "python_version": version,
                    "revision_sha": revision,
                    "tree_sha": tree,
                }
            )
            write_json(target / "manifest.json", manifest)
        _, proof = artifacts.load_version(root, version)
        combined = root / f"renewal-coverage-py{version}"
        combined.mkdir()
        xml = combined / "coverage.xml"
        xml.write_text('<coverage lines-valid="1" lines-covered="1" line-rate="1"/>')
        proof.update(
            {
                "coverage_percent": 100,
                "coverage_sha256": hashlib.sha256(xml.read_bytes()).hexdigest(),
            }
        )
        write_json(combined / "report.json", proof)


def rewrite_payload(
    root: Path,
    name: str,
    change: Callable[[JsonObject], None],
) -> None:
    """Mutate semantics and rebind the bytes, so checksums cannot mask a detector."""
    path = root / name
    value, _ = load_json(path)
    change(value)
    write_json(path, value)
    manifest, _ = load_json(root / "manifest.json")
    hashes = manifest["files"]
    assert isinstance(hashes, dict)
    hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(root / "manifest.json", manifest)
