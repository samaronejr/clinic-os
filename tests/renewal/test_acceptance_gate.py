"""Negative tests for the aggregate renewal acceptance verdict.

The gate must reject failed/cancelled/skipped required jobs, shrunken or
duplicated suite binding, unknown or malformed ``suite@engine`` shard legs,
missing or malformed runner reports/JUnit files, reports naming another
engine, zero-test evidence, skipped required tests and diverging source
revisions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ops.testing.renewal_acceptance import (
    REQUIRED_JOBS,
    _binding_failures,
    artifact_label,
    validate,
)
from ops.testing.renewal_runner import CHROMIUM_ONLY_SUITES, SUITES

REPOSITORY = Path(__file__).resolve().parents[2]

FIRST_SUITE = sorted(SUITES)[0]

WORKFLOW = """\
jobs:
  renewal-browser:
    strategy:
      matrix:
        shard:
%s
  renewal-acceptance:
    needs: [test, contracts, renewal-browser, worker-integration,
            migration-upgrade, actionlint]
"""


def _workflow(suites: list[str] | None = None) -> str:
    shard = "\n".join(f'          - "{suite}"' for suite in (suites or sorted(SUITES)))
    return WORKFLOW % shard


def _needs(**overrides: object) -> dict[str, object]:
    needs: dict[str, object] = {job: {"result": "success"} for job in REQUIRED_JOBS}
    needs.update(overrides)
    return needs


def _report(  # noqa: PLR0913 - fixture builder needs its switches
    suite: str,
    *,
    engine: str = "chromium",
    digest: str = "d" * 64,
    tests: int = 3,
    revision: str = "a" * 40,
    tree: str = "b" * 40,
) -> dict[str, object]:
    return {
        "artifact_root": "/tmp/evidence",  # noqa: S108 - fixture-only label
        "base_url": "http://127.0.0.1:54321",
        "browser": "/usr/bin/chromium",
        "engine": engine,
        "pytest_exit": 0,
        "revision_sha": revision,
        "runtime_role": "clinic_app",
        "schema_version": 1,
        "source_entry_count": 1000,
        "source_manifest_sha256": digest,
        "suite": suite,
        "tests": tests,
        "tree_sha": tree,
        "verified_record": None,
    }


def _write_suite(  # noqa: PLR0913 - fixture builder needs its switches
    root: Path,
    suite: str,
    *,
    report: dict[str, object] | None = None,
    junit: str | None = None,
    raw_report: str | None = None,
    engine: str = "chromium",
    digest: str = "d" * 64,
    revision: str = "a" * 40,
    tree: str = "b" * 40,
) -> None:
    suite_dir = root / "shard" / artifact_label(suite, engine)
    (suite_dir / "browser").mkdir(parents=True, exist_ok=True)
    report = (
        report
        if report is not None
        else _report(suite, engine=engine, digest=digest, revision=revision, tree=tree)
    )
    if raw_report is not None:
        (suite_dir / "report.json").write_text(raw_report)
    else:
        (suite_dir / "report.json").write_text(json.dumps(report))
    if junit is None:
        raw_tests = report.get("tests", 3) if isinstance(report, dict) else 3
        tests = raw_tests if isinstance(raw_tests, int) else 3
        cases = "".join(
            f'<testcase name="t{i}" classname="{suite}"/>' for i in range(tests)
        )
        junit = (
            f'<testsuite name="{suite}" tests="{tests}" failures="0" '
            f'errors="0" skipped="0">{cases}</testsuite>'
        )
    (suite_dir / "browser" / f"junit-{suite}.xml").write_text(junit)


def _populate(
    root: Path,
    *,
    digest: str = "d" * 64,
    revision: str = "a" * 40,
    tree: str = "b" * 40,
) -> Path:
    for suite in sorted(SUITES):
        _write_suite(root, suite, digest=digest, revision=revision, tree=tree)
    return root


def test_complete_evidence_is_accepted(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    assert validate(_workflow(), _needs(), artifacts) == []


@pytest.mark.parametrize(
    ("job", "result"),
    [("test", "failure"), ("contracts", "cancelled"), ("renewal-browser", "skipped")],
)
def test_unsuccessful_required_job_is_rejected(
    tmp_path: Path, job: str, result: str
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    failures = validate(_workflow(), _needs(**{job: {"result": result}}), artifacts)
    assert any(job in failure for failure in failures)


def test_missing_required_job_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    needs = _needs()
    del needs["worker-integration"]
    failures = validate(_workflow(), needs, artifacts)
    assert any("worker-integration" in failure for failure in failures)


def test_undeclared_need_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    workflow = _workflow().replace("migration-upgrade, ", "")
    failures = validate(workflow, _needs(), artifacts)
    assert any("migration-upgrade" in failure for failure in failures)


def test_dropped_suite_is_rejected(tmp_path: Path) -> None:
    suites = sorted(SUITES)
    suites.remove(FIRST_SUITE)
    artifacts = _populate(tmp_path / "artifacts")
    failures = validate(_workflow(suites), _needs(), artifacts)
    assert any(FIRST_SUITE in failure for failure in failures)


def test_unregistered_suite_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    failures = validate(
        _workflow([*sorted(SUITES), "phantom-suite"]), _needs(), artifacts
    )
    assert any("phantom-suite" in failure for failure in failures)


def test_duplicate_suite_binding_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    failures = validate(_workflow([FIRST_SUITE, *sorted(SUITES)]), _needs(), artifacts)
    assert any("more than once" in failure for failure in failures)


def test_missing_report_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    (artifacts / "shard" / FIRST_SUITE / "report.json").unlink()
    failures = validate(_workflow(), _needs(), artifacts)
    assert any(FIRST_SUITE in failure for failure in failures)


def test_malformed_report_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    _write_suite(artifacts, FIRST_SUITE, raw_report="{ not json")
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("malformed" in failure for failure in failures)


def test_zero_test_report_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    _write_suite(
        artifacts,
        FIRST_SUITE,
        report=_report(FIRST_SUITE, tests=0),
        junit=(
            f'<testsuite name="{FIRST_SUITE}" tests="0" failures="0" '
            'errors="0" skipped="0"/>'
        ),
    )
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("zero executed tests" in failure for failure in failures)


def test_failed_pytest_exit_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    report = _report(FIRST_SUITE)
    report["pytest_exit"] = 1
    _write_suite(artifacts, FIRST_SUITE, report=report)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("pytest exit" in failure for failure in failures)


def test_wrong_runtime_role_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    report = _report(FIRST_SUITE)
    report["runtime_role"] = "clinic_owner"
    _write_suite(artifacts, FIRST_SUITE, report=report)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("runtime role" in failure for failure in failures)


def test_missing_digest_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    report = _report(FIRST_SUITE)
    report["source_manifest_sha256"] = ""
    _write_suite(artifacts, FIRST_SUITE, report=report)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("digest" in failure for failure in failures)


def test_missing_revision_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    report = _report(FIRST_SUITE)
    report["revision_sha"] = ""
    _write_suite(artifacts, FIRST_SUITE, report=report)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("revision_sha" in failure for failure in failures)


def test_skipped_junit_case_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    junit = (
        f'<testsuite name="{FIRST_SUITE}" tests="3" failures="0" errors="0" '
        'skipped="1"><testcase name="t0"/><testcase name="t1"/>'
        '<testcase name="t2"><skipped/></testcase></testsuite>'
    )
    _write_suite(artifacts, FIRST_SUITE, junit=junit)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("skipped" in failure for failure in failures)


def test_junit_report_mismatch_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    junit = (
        f'<testsuite name="{FIRST_SUITE}" tests="9" failures="0" errors="0" '
        'skipped="0"><testcase name="t0"/></testsuite>'
    )
    _write_suite(artifacts, FIRST_SUITE, junit=junit)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("count" in failure for failure in failures)


def test_malformed_junit_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    _write_suite(artifacts, FIRST_SUITE, junit="<testsuite")
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("malformed" in failure for failure in failures)


def test_diverging_source_revisions_are_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts", revision="c" * 40)
    second = sorted(SUITES)[1]
    _write_suite(artifacts, second, revision="e" * 40)
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("revision" in failure for failure in failures)


def test_diverging_manifest_digests_are_accepted(tmp_path: Path) -> None:
    # The manifest digest binds per-job ledger state; only revision and
    # tree identity must agree across shards.
    artifacts = _populate(tmp_path / "artifacts", digest="a" * 64)
    second = sorted(SUITES)[1]
    _write_suite(artifacts, second, digest="b" * 64)
    assert validate(_workflow(), _needs(), artifacts) == []


def test_missing_workflow_matrix_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    workflow = "jobs:\n  renewal-acceptance:\n    needs: [test]\n"
    failures = validate(workflow, _needs(), artifacts)
    assert any("matrix" in failure for failure in failures)


ENGINE_LEGS = ["smoke@firefox", "smoke@webkit"]


def _populate_legs(root: Path) -> Path:
    _populate(root)
    for engine in ("firefox", "webkit"):
        _write_suite(root, "smoke", engine=engine)
    return root


def test_engine_legs_with_their_own_reports_are_accepted(tmp_path: Path) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    workflow = _workflow([*sorted(SUITES), *ENGINE_LEGS])
    assert validate(workflow, _needs(), artifacts) == []


def test_explicit_chromium_leg_is_the_bare_suite(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    suites = [s if s != "smoke" else "smoke@chromium" for s in sorted(SUITES)]
    assert validate(_workflow(suites), _needs(), artifacts) == []
    failures = validate(
        _workflow([*sorted(SUITES), "smoke@chromium"]), _needs(), artifacts
    )
    assert any("smoke is bound more than once" in failure for failure in failures)


@pytest.mark.parametrize("engine", ["netscape", "Firefox", "chrome", "safari"])
def test_unknown_engine_leg_is_rejected(tmp_path: Path, engine: str) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    workflow = _workflow([*sorted(SUITES), f"smoke@{engine}"])
    failures = validate(workflow, _needs(), artifacts)
    assert f"shard entry smoke@{engine} names unknown engine {engine}" in failures


@pytest.mark.parametrize("entry", ["smoke@", "@firefox", "smoke@firefox@webkit"])
def test_malformed_engine_leg_is_rejected(tmp_path: Path, entry: str) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    failures = validate(_workflow([*sorted(SUITES), entry]), _needs(), artifacts)
    assert f"shard entry {entry} is malformed" in failures


def test_duplicate_engine_leg_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    workflow = _workflow([*sorted(SUITES), *ENGINE_LEGS, "smoke@firefox"])
    failures = validate(workflow, _needs(), artifacts)
    assert "suite smoke@firefox is bound more than once" in failures


def test_engine_leg_of_unregistered_suite_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    workflow = _workflow([*sorted(SUITES), "phantom-suite@webkit"])
    failures = validate(workflow, _needs(), artifacts)
    assert "shard binds unregistered suite phantom-suite" in failures


def test_chromium_only_suite_on_another_engine_is_rejected(tmp_path: Path) -> None:
    suite = sorted(CHROMIUM_ONLY_SUITES)[0]
    artifacts = _populate(tmp_path / "artifacts")
    _write_suite(artifacts, suite, engine="webkit")
    workflow = _workflow([*sorted(SUITES), f"{suite}@webkit"])
    failures = validate(workflow, _needs(), artifacts)
    assert f"shard entry {suite}@webkit runs a Chromium-only suite on webkit" in (
        failures
    )


def test_engine_leg_never_replaces_the_chromium_binding(tmp_path: Path) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    suites = [s for s in sorted(SUITES) if s != "smoke"]
    failures = validate(_workflow([*suites, *ENGINE_LEGS]), _needs(), artifacts)
    assert "registered suite smoke is not executed in CI" in failures


def test_missing_engine_leg_report_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    _write_suite(artifacts, "smoke", engine="firefox")
    workflow = _workflow([*sorted(SUITES), *ENGINE_LEGS])
    failures = validate(workflow, _needs(), artifacts)
    assert "suite smoke@webkit: expected exactly one report, found 0" in failures


def test_engine_leg_report_naming_another_engine_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    _write_suite(
        artifacts, "smoke", engine="webkit", report=_report("smoke", engine="chromium")
    )
    workflow = _workflow([*sorted(SUITES), *ENGINE_LEGS])
    failures = validate(workflow, _needs(), artifacts)
    assert "suite smoke@webkit: report names a different engine" in failures


def test_report_without_engine_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    report = _report(FIRST_SUITE)
    del report["engine"]
    _write_suite(artifacts, FIRST_SUITE, report=report)
    failures = validate(_workflow(), _needs(), artifacts)
    assert f"suite {FIRST_SUITE}: report names a different engine" in failures


def test_failing_engine_leg_is_rejected(tmp_path: Path) -> None:
    artifacts = _populate_legs(tmp_path / "artifacts")
    report = _report("smoke", engine="firefox")
    report["pytest_exit"] = 1
    _write_suite(artifacts, "smoke", engine="firefox", report=report)
    workflow = _workflow([*sorted(SUITES), *ENGINE_LEGS])
    failures = validate(workflow, _needs(), artifacts)
    assert "suite smoke@firefox: pytest exit is not zero" in failures


def test_tracked_workflow_binds_every_suite_plus_firefox_and_webkit_legs() -> None:
    workflow = (REPOSITORY / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    failures, legs = _binding_failures(workflow, set(SUITES))
    assert failures == []
    assert legs == {(suite, "chromium") for suite in SUITES} | {
        ("smoke", "firefox"),
        ("smoke", "webkit"),
    }
