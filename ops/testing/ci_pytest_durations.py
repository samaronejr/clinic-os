"""Regenerate the committed per-file durations that weight the shard split.

    python -m ops.testing.ci_pytest_durations --source hosted --note TEXT BUNDLE...

Each BUNDLE is one shard's retained evidence from a single green run of one
Python version (``ci_pytest --shard N --artifact-root DIR``; hosted, the
``renewal-python-py3.12-shard-N`` artifacts). Its reports carry every test's
setup, call and teardown seconds. A file weighs, per phase, the sum of its
tests' seconds in whole milliseconds; a file the table does not know falls back
to its test count times the median test's milliseconds. A bundle that records a
failure or a nonzero exit, bundles of different revisions and a file phase
measured twice are refused. The table is written to
``ops/testing/ci-pytest-durations.json`` (``--output`` elsewhere) and its
SHA-256 printed: put it in ``DURATIONS_SHA256``.
"""

from __future__ import annotations

import argparse
import hashlib
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.ci_pytest_artifacts import report_durations
from ops.testing.ci_pytest_partition import (
    DURATION_SOURCES,
    DURATIONS,
    PHASES,
    encode_durations,
    parse_durations,
)
from ops.testing.isolation_common import IsolationError, load_json

if TYPE_CHECKING:
    from collections.abc import Sequence


def _fail(message: str) -> Never:
    raise IsolationError(message)


def measure(bundles: Sequence[Path]) -> tuple[str, dict[str, dict[str, int]], int]:
    """Return the run's revision, per-file milliseconds and per-test fallback."""
    revisions: set[str] = set()
    seconds: dict[str, dict[str, float]] = defaultdict(dict)
    tests: list[float] = []
    for bundle in bundles:
        manifest, _ = load_json(bundle / "manifest.json")
        exits = manifest.get("exits")
        if exits != {"parallel": 0, "serial": 0} or manifest.get("failures") != []:
            _fail(f"{bundle.name}: not a green shard bundle")
        revision = manifest.get("revision_sha")
        if not isinstance(revision, str) or not revision:
            _fail(f"{bundle.name}: manifest records no revision")
        revisions.add(revision)
        for phase in PHASES:
            totals: dict[str, float] = defaultdict(float)
            for nodeid, spent in report_durations(
                bundle / f"{phase}-reports.json"
            ).items():
                totals[nodeid.split("::", 1)[0]] += spent
                tests.append(spent)
            for name, spent in totals.items():
                if phase in seconds[name]:
                    _fail(f"{name} ({phase}) is measured in two bundles")
                seconds[name][phase] = spent
    if len(revisions) != 1:
        _fail("bundles come from different revisions")
    if not tests:
        _fail("bundles report no tests")
    files = {
        name: {phase: round(spent * 1000) for phase, spent in phases.items()}
        for name, phases in seconds.items()
    }
    return revisions.pop(), files, max(1, round(statistics.median(tests) * 1000))


def main(argv: Sequence[str] | None = None) -> int:
    """Write the table from the bundles; exit 2 when any bundle is refused."""
    parser = argparse.ArgumentParser(prog="python -m ops.testing.ci_pytest_durations")
    parser.add_argument("--source", choices=DURATION_SOURCES, required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--output", type=Path, default=DURATIONS)
    parser.add_argument("bundles", nargs="+", type=Path)
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        revision, files, fallback = measure(arguments.bundles)
        raw = encode_durations(
            source=arguments.source,
            note=arguments.note,
            revision_sha=revision,
            fallback_ms=fallback,
            files=files,
        )
        digest = hashlib.sha256(raw).hexdigest()
        parse_durations(raw, sha256=digest)
    except IsolationError as error:
        sys.stderr.write(f"ci-pytest-durations: {error}\n")
        return 2
    arguments.output.write_bytes(raw)
    sys.stdout.write(
        f"ci-pytest-durations: wrote {arguments.output} files={len(files)} "
        f"fallback_ms_per_test={fallback} sha256={digest}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
