"""Aggregate verdict for the renewal release-candidate acceptance gate.

``python -m ops.testing.renewal_acceptance`` validates the hosted evidence:

* every required dependency job reported ``success`` (a failed, cancelled,
  missing or skipped job rejects acceptance);
* the workflow's ``renewal-browser`` matrix still binds every registered
  ``ops.testing.renewal_runner`` suite on Chromium — a removed suite or a
  shard that silently drops one shrinks verification and must fail. A shard
  entry is ``suite`` (Chromium) or ``suite@engine`` with engine one of
  ``chromium``, ``firefox`` or ``webkit``; an unknown engine, a malformed
  entry or a duplicate (suite, engine) leg is rejected;
* every bound leg produced a runner report (artifact directory ``suite`` for
  Chromium, ``suite@engine`` otherwise) naming the suite and engine, with a
  nonzero executed test count, a zero exit, the ``clinic_app`` runtime role
  and a bound source manifest; all shards must agree on one tested source
  revision
  (the manifest digest itself binds per-job ledger state, so it cannot
  be compared across jobs);
* every suite's JUnit document parses and carries no failures, errors or
  skips, and its case count matches the runner report.

Exits 0 with a JSON summary, 2 with a failure list on stderr.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Final

from ops.testing.renewal_runner import DEFAULT_ENGINE, ENGINES, SUITES

REQUIRED_JOBS: Final = (
    "test",
    "contracts",
    "renewal-browser",
    "worker-integration",
    "migration-upgrade",
    "actionlint",
)
SHARD_LIST = re.compile(
    r'renewal-browser:.*?shard:\s*\n((?:\s*-\s*"[^"]*"\s*\n)+)', re.DOTALL
)
SHARD_ENTRY = re.compile(r'-\s*"([^"]*)"')
NEEDS_LIST = re.compile(r"renewal-acceptance:.*?needs:\s*\[([^\]]*)\]", re.DOTALL)
DIGEST_HEX_LENGTH: Final = 64
SHA_HEX_LENGTH: Final = 40
EXPECTED_OPTIONS: Final = 3


def _shard_entries(workflow: str) -> tuple[list[str], list[str]]:
    """Return (declared shard entries, parse failures) from the workflow text."""
    match = SHARD_LIST.search(workflow)
    if match is None:
        return [], ["workflow renewal-browser shard matrix is missing"]
    entries = [
        entry
        for shard in SHARD_ENTRY.findall(match.group(1))
        for entry in shard.split(",")
    ]
    return [entry.strip() for entry in entries if entry.strip()], []


def _leg(entry: str) -> tuple[str, str] | None:
    """Split ``suite`` / ``suite@engine`` into (suite, engine); None if malformed."""
    suite, separator, engine = entry.partition("@")
    if not separator:
        return suite, DEFAULT_ENGINE
    if not suite or not engine or "@" in engine:
        return None
    return suite, engine


def artifact_label(suite: str, engine: str) -> str:
    """Return the per-leg artifact directory name the CI shard writes."""
    return suite if engine == DEFAULT_ENGINE else f"{suite}@{engine}"


def _declared_needs(workflow: str) -> list[str]:
    match = NEEDS_LIST.search(workflow)
    if match is None:
        return []
    return [
        name.strip()
        for name in match.group(1).split(",")
        if name.strip() and name.strip() != "renewal-acceptance"
    ]


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    """Return (tests, failures, errors, skipped) aggregated across suites."""
    root = ET.parse(path).getroot()  # noqa: S314 - runner-owned file.
    tests = failures = errors = skipped = 0
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        tests += int(suite.get("tests", "0"))
        failures += int(suite.get("failures", "0"))
        errors += int(suite.get("errors", "0"))
        skipped += int(suite.get("skipped", "0"))
    return tests, failures, errors, skipped


def _junit_failures(suite: str, junit: Path, tests: int | None) -> list[str]:
    if not junit.is_file():
        return [f"suite {suite}: junit report missing"]
    try:
        jt, jf, je, js = _junit_counts(junit)
    except ET.ParseError:
        return [f"suite {suite}: junit report is malformed"]
    failures: list[str] = []
    if jt <= 0:
        failures.append(f"suite {suite}: junit ran zero tests")
    if jf or je:
        failures.append(f"suite {suite}: junit has failures or errors")
    if js:
        failures.append(f"suite {suite}: junit skipped required tests")
    if tests is not None and jt != tests:
        failures.append(f"suite {suite}: junit count {jt} != report count {tests}")
    return failures


def _validate_suite_report(suite: str, engine: str, report_path: Path) -> list[str]:
    failures: list[str] = []
    label = artifact_label(suite, engine)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"suite {label}: report is missing or malformed"]
    if not isinstance(report, dict):
        return [f"suite {label}: report is not an object"]
    if report.get("suite") != suite:
        failures.append(f"suite {label}: report names a different suite")
    if report.get("engine") != engine:
        failures.append(f"suite {label}: report names a different engine")
    junit = report_path.parent / "browser" / f"junit-{suite}.xml"
    failures.extend(_report_fields(label, report, junit))
    return failures


def _report_fields(label: str, report: dict[str, object], junit: Path) -> list[str]:
    failures: list[str] = []
    if report.get("pytest_exit") != 0:
        failures.append(f"suite {label}: pytest exit is not zero")
    tests = report.get("tests")
    if not isinstance(tests, int) or tests <= 0:
        failures.append(f"suite {label}: zero executed tests")
        tests = None
    if report.get("runtime_role") != "clinic_app":
        failures.append(f"suite {label}: runtime role is not clinic_app")
    digest = report.get("source_manifest_sha256")
    if not isinstance(digest, str) or len(digest) != DIGEST_HEX_LENGTH:
        failures.append(f"suite {label}: source manifest digest missing")
    for field in ("revision_sha", "tree_sha"):
        value = report.get(field)
        if not isinstance(value, str) or len(value) != SHA_HEX_LENGTH:
            failures.append(f"suite {label}: source {field} missing")
    failures.extend(_junit_failures(label, junit, tests))
    return failures


def _needs_failures(needs: dict[str, object]) -> list[str]:
    failures: list[str] = []
    for job in REQUIRED_JOBS:
        entry = needs.get(job)
        if not isinstance(entry, dict) or entry.get("result") != "success":
            result = (
                entry.get("result") if isinstance(entry, dict) else None
            ) or "missing"
            failures.append(f"required job {job} did not succeed ({result})")
    return failures


def _leg_failure(entry: str, registered: set[str]) -> tuple[str, str] | str:
    """Return the (suite, engine) leg of ``entry`` or why it is rejected."""
    leg = _leg(entry)
    if leg is None:
        return f"shard entry {entry} is malformed"
    suite, engine = leg
    if engine not in ENGINES:
        return f"shard entry {entry} names unknown engine {engine}"
    if suite not in registered:
        return f"shard binds unregistered suite {suite}"
    return leg


def _binding_failures(
    workflow: str, registered: set[str]
) -> tuple[list[str], set[tuple[str, str]]]:
    """Return (failures, bound (suite, engine) legs) for the workflow contract."""
    failures: list[str] = []
    declared = _declared_needs(workflow)
    failures.extend(
        f"acceptance job does not declare need on {job}"
        for job in REQUIRED_JOBS
        if job not in declared
    )
    entries, parse_failures = _shard_entries(workflow)
    failures.extend(parse_failures)
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        leg = _leg_failure(entry, registered)
        if isinstance(leg, str):
            failures.append(leg)
            continue
        if leg in seen:
            failures.append(f"suite {artifact_label(*leg)} is bound more than once")
        seen.add(leg)
    failures.extend(
        f"registered suite {suite} is not executed in CI"
        for suite in sorted(registered)
        if (suite, DEFAULT_ENGINE) not in seen
    )
    return failures, seen


def _report_failures(
    legs: set[tuple[str, str]], artifacts: Path
) -> tuple[list[str], set[tuple[str, str]]]:
    """Return (failures, tested (revision, tree) identities) across reports."""
    failures: list[str] = []
    identities: set[tuple[str, str]] = set()
    for suite, engine in sorted(legs):
        label = artifact_label(suite, engine)
        candidates = list(artifacts.glob(f"*/{label}/report.json"))
        if len(candidates) != 1:
            failures.append(
                f"suite {label}: expected exactly one report, found {len(candidates)}"
            )
            continue
        suite_failures = _validate_suite_report(suite, engine, candidates[0])
        failures.extend(suite_failures)
        if not suite_failures:
            report = json.loads(candidates[0].read_text(encoding="utf-8"))
            identities.add((str(report["revision_sha"]), str(report["tree_sha"])))
    return failures, identities


def validate(workflow: str, needs: dict[str, object], artifacts: Path) -> list[str]:
    """Return every acceptance failure; an empty list means accepted."""
    registered = set(SUITES)
    failures = _needs_failures(needs)
    binding, bound = _binding_failures(workflow, registered)
    failures.extend(binding)
    # Every registered suite owes a Chromium report even when its binding is
    # missing; every additional engine leg owes its own report too.
    legs = {(suite, DEFAULT_ENGINE) for suite in registered} | bound
    report_failures, identities = _report_failures(legs, artifacts)
    failures.extend(report_failures)
    if not failures and len(identities) != 1:
        failures.append("suite reports disagree on the tested source revision")
    return failures


def main(argv: list[str] | None = None) -> int:
    """Validate hosted renewal evidence and exit 0/2."""
    arguments = argv if argv is not None else sys.argv[1:]
    options: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        name = arguments[index]
        if name not in {"--workflow", "--needs-json", "--artifacts"} or (
            index + 1 >= len(arguments) or arguments[index + 1].startswith("--")
        ):
            sys.stderr.write("renewal-acceptance: invalid arguments\n")
            return 2
        options[name] = arguments[index + 1]
        index += 2
    if len(options) != EXPECTED_OPTIONS:
        sys.stderr.write("renewal-acceptance: missing required arguments\n")
        return 2
    try:
        needs_raw = json.loads(
            Path(options["--needs-json"]).read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        sys.stderr.write("renewal-acceptance: needs payload is malformed\n")
        return 2
    failures = validate(
        Path(options["--workflow"]).read_text(encoding="utf-8"),
        needs_raw if isinstance(needs_raw, dict) else {},
        Path(options["--artifacts"]),
    )
    if failures:
        for failure in failures:
            sys.stderr.write(f"renewal-acceptance: {failure}\n")
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "schema_version": 1,
                "suites": sorted(SUITES),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
