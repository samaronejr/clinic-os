"""Negative tests for the aggregate renewal acceptance verdict.

The gate must reject failed/cancelled/skipped required jobs, shrunken or
duplicated suite binding, unknown or malformed ``suite@engine`` shard legs,
missing or malformed runner reports/JUnit files, reports naming another
engine, zero-test evidence, skipped required tests and diverging source
revisions.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from ops.testing import ci_hosted_gate
from ops.testing.ci_pidfd_producers import Producer, select_producers
from ops.testing.ci_pytest_partition import SHARDS
from ops.testing.pidfd_preflight import CAPABILITIES, RECORD_NAME
from ops.testing.renewal_acceptance import (
    PREFLIGHT_STEP,
    REQUIRED_JOBS,
    _binding_failures,
    artifact_label,
    validate,
)
from ops.testing.renewal_runner import SUITES
from ops.testing.validate_action_pins import SNAPSHOT

from infra.ci_shard_support import write_python_artifacts

REPOSITORY = Path(__file__).resolve().parents[2]

FIRST_SUITE = sorted(SUITES)[0]


def _workflow(suites: list[str] | None = None) -> str:
    shard = "\n".join(f'          - "{suite}"' for suite in (suites or sorted(SUITES)))
    workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text()
    return re.sub(r'(?m)(?:^          - "[^"]*"\n)+', shard + "\n", workflow, count=1)


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


def _write_preflight(
    directory: Path, job: str, slot: str, minor: str, revision: str
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _ = (directory / RECORD_NAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "result": "PASS",
                "job": job,
                "slot": slot,
                "revision_sha": revision,
                "run_id": "1234",
                "run_attempt": 1,
                "artifact": (
                    directory.parent.name
                    if job == "renewal-browser"
                    else directory.name
                ),
                "interpreter": "/usr/bin/python",
                "version": {"3.12": "3.12.13", "3.13": "3.13.14"}[minor],
                "build_sha256": "a" * 64,
                "capabilities": dict.fromkeys(CAPABILITIES, True),
            },
        ),
    )


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
    _write_preflight(
        suite_dir, "renewal-browser", artifact_label(suite, engine), "3.12", revision
    )
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
    write_python_artifacts(root, revision=revision, tree=tree)
    for job in ("contracts", "worker-integration", "migration-upgrade", "actionlint"):
        _write_preflight(root / f"renewal-{job}", job, job, "3.12", revision)
    for version in ("3.12", "3.13"):
        _write_preflight(
            root / f"renewal-coverage-py{version}",
            "python-coverage",
            version,
            version,
            revision,
        )
        for shard in SHARDS:
            _write_preflight(
                root / f"renewal-python-py{version}-shard-{shard}",
                "test",
                str(shard),
                version,
                revision,
            )
    return root


def test_complete_evidence_is_accepted(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    assert validate(_workflow(), _needs(), artifacts) == []


@pytest.mark.parametrize("mutation", ["attempt-swap", "cross-job", "wrong-sha"])
def test_acceptance_binds_effective_actions_producer(
    tmp_path: Path, mutation: str
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    target = artifacts / "shard" / FIRST_SUITE / RECORD_NAME
    producers: dict[str, Producer] = {
        directory.name: {"attempt": 1, "job_id": index, "artifact_id": index}
        for index, directory in enumerate(sorted(artifacts.iterdir()), 1)
    }
    producers["shard"]["attempt"] = 2
    stale = target.read_bytes()
    for path in (artifacts / "shard").glob(f"*/{RECORD_NAME}"):
        record = json.loads(path.read_bytes())
        record["run_attempt"] = 2
        path.write_text(json.dumps(record))
    source = ("a" * 40, "1234")
    assert (
        validate(
            _workflow(), _needs(), artifacts, hosted_source=source, producers=producers
        )
        == []
    )
    if mutation == "attempt-swap":
        target.write_bytes(stale)
    elif mutation == "cross-job":
        target.write_bytes((artifacts / "renewal-contracts" / RECORD_NAME).read_bytes())
    else:
        record = json.loads(target.read_bytes())
        record["revision_sha"] = "f" * 40
        target.write_text(json.dumps(record))
    assert (
        f"pidfd preflight renewal-browser/{FIRST_SUITE}: missing or invalid PASS"
        in validate(
            _workflow(), _needs(), artifacts, hosted_source=source, producers=producers
        )
    )


def test_producer_selector_uses_original_artifact_for_carried_jobs() -> None:
    head = "a" * 40
    first = {
        "name": "first",
        "id": 1,
        "run_id": 1234,
        "head_sha": head,
        "started_at": "2026-11-28T10:00:00Z",
        "completed_at": "2026-11-28T10:05:00Z",
    }
    second = {**first, "name": "second", "id": 2}
    retried = {
        **second,
        "id": 4,
        "started_at": "2026-11-28T11:00:00Z",
        "completed_at": "2026-11-28T11:05:00Z",
    }
    artifact = {
        "expired": False,
        "created_at": "2026-11-28T10:04:00Z",
        "workflow_run": {"id": 1234, "head_sha": head},
    }
    selected = select_producers(
        {"renewal-first": "first", "renewal-second": "second"},
        [[first, second], [{**first, "id": 3}, retried]],
        [
            {**artifact, "name": "renewal-first", "id": 10},
            {**artifact, "name": "renewal-second", "id": 11},
            {
                **artifact,
                "name": "renewal-second",
                "created_at": "2026-11-28T11:04:00Z",
                "id": 12,
            },
        ],
        run_id=1234,
        head=head,
    )
    assert selected == {
        "renewal-first": {"attempt": 1, "job_id": 1, "artifact_id": 10},
        "renewal-second": {"attempt": 2, "job_id": 4, "artifact_id": 12},
    }


@pytest.mark.parametrize("mutation", ["missing", "FAIL", "wrong-slot", "wrong-run"])
def test_acceptance_requires_matching_pidfd_evidence(
    tmp_path: Path, mutation: str
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    record = artifacts / "renewal-python-py3.13-shard-2" / RECORD_NAME
    if mutation == "missing":
        record.unlink()
    else:
        payload = json.loads(record.read_text())
        match mutation:
            case "FAIL":
                payload["result"] = "FAIL"
            case "wrong-slot":
                payload["slot"] = "3"
            case "wrong-run":
                payload["run_id"] = "42"
        _ = record.write_text(json.dumps(payload))
    assert any(
        failure.startswith("pidfd preflight")
        for failure in validate(_workflow(), _needs(), artifacts)
    )


def test_successful_driver_that_skips_preflight_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    destination = artifacts / "renewal-actionlint"
    (destination / RECORD_NAME).unlink()
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_JOB": "actionlint",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "1",
        "CLINIC_CI_ARTIFACT": "renewal-actionlint",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["ci_hosted_gate", "actionlint", str(destination)])
    monkeypatch.setattr(ci_hosted_gate, "require_pidfd_at_startup", dict)

    assert ci_hosted_gate.main() == 0
    assert "pidfd preflight actionlint/actionlint: missing or invalid PASS" in validate(
        _workflow(), _needs(), artifacts
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("if", "${{ false }}"),
        ("continue-on-error", "true"),
        ("shell", "bash"),
        ("env", "{}"),
    ],
)
def test_verdict_rejects_modified_preflight_step(
    tmp_path: Path, key: str, value: str
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    guard = "\n".join(PREFLIGHT_STEP)
    workflow = _workflow().replace(guard, guard + f"\n        {key}: {value}", 1)
    assert "workflow test: missing unconditional pidfd preflight" in validate(
        workflow, _needs(), artifacts
    )


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("delete", "missing unconditional pidfd preflight"),
        ("before", "missing unconditional pidfd preflight"),
        ("after", "missing unconditional pidfd preflight"),
        ("new-job", "workflow jobs do not match"),
        ("always()", "step can run after failure"),
        ("failure()", "step can run after failure"),
        ("cancelled()", "step can run after failure"),
        ("step-ignore", "run step ignores errors"),
        ("job-ignore", "overrides job execution defaults"),
        ("job-defaults", "overrides job execution defaults"),
        ("workflow-defaults", "workflow sets defaults"),
    ],
)
def test_verdict_rejects_workflow_guard_mutations(
    tmp_path: Path, mutation: str, diagnostic: str
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    workflow = _workflow()
    guard = "\n".join(PREFLIGHT_STEP) + "\n"
    workload = "      - name: Install PDF inspection prerequisites\n"
    match mutation:
        case "delete":
            workflow = workflow.replace(guard, "", 1)
        case "before":
            workflow = workflow.replace(guard, "", 1).replace(
                "      - name: Authenticate repository snapshot\n",
                guard + "      - name: Authenticate repository snapshot\n",
                1,
            )
        case "after":
            workflow = workflow.replace(guard, "", 1).replace(
                workload, "      - run: pytest\n" + guard + workload, 1
            )
        case "new-job":
            workflow += (
                f"\n  added:\n    steps:\n      - run: {SNAPSHOT}\n"
                "      - run: pytest\n"
            )
        case "always()" | "failure()" | "cancelled()":
            workflow = workflow.replace(
                workload, workload + f"        if: ${{{{ {mutation} }}}}\n", 1
            )
        case "step-ignore":
            workflow = workflow.replace(
                workload, workload + "        continue-on-error: true\n", 1
            )
        case "job-ignore":
            workflow = workflow.replace(
                "  test:\n", "  test:\n    continue-on-error: true\n", 1
            )
        case "job-defaults":
            workflow = workflow.replace(
                "  test:\n", "  test:\n    defaults: {run: {shell: bash}}\n", 1
            )
        case "workflow-defaults":
            workflow = "defaults: {run: {shell: bash}}\n" + workflow
    assert any(
        diagnostic in failure for failure in validate(workflow, _needs(), artifacts)
    )


@pytest.mark.parametrize(
    ("step", "diagnostic"),
    [
        ("      - if: failure()\n        run: pytest\n", "step can run after failure"),
        (
            "      - name: Work\n        run: pytest\n"
            "        if: >-\n          failure()\n",
            "step can run after failure",
        ),
        ('      - run: pytest\n        "if": always()\n', "step can run after failure"),
        (
            '      - run: pytest\n        "continue-on-error": true\n',
            "run step ignores errors",
        ),
        (
            '      - {run: pytest, "if": "${{ always() }}"}\n',
            "step can run after failure",
        ),
        (
            '      - {run: pytest, "continue-on-error": true}\n',
            "run step ignores errors",
        ),
        (
            "      - name: &after failure()\n        run: echo setup\n"
            "      - {run: pytest, if: *after}\n",
            "step can run after failure",
        ),
        (
            "      - uses: actions/upload-artifact@fixture\n"
            "        continue-on-error: &ignore true\n"
            "      - {run: pytest, continue-on-error: *ignore}\n",
            "run step ignores errors",
        ),
    ],
)
def test_verdict_refuses_unsupported_step_syntax(
    tmp_path: Path, step: str, diagnostic: str
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    workload = "      - name: Install PDF inspection prerequisites\n"
    workflow = _workflow().replace(workload, step + workload, 1)
    assert f"workflow test: {diagnostic}" in validate(workflow, _needs(), artifacts)


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


@pytest.mark.parametrize("engine", ["firefox", "webkit"])
def test_formerly_chromium_only_suites_bind_engine_legs(
    tmp_path: Path, engine: str
) -> None:
    ported = ["consent", "patient-video", "clinical-history"]
    artifacts = _populate(tmp_path / "artifacts")
    for suite in ported:
        _write_suite(artifacts, suite, engine=engine)
    workflow = _workflow([*sorted(SUITES), *(f"{s}@{engine}" for s in ported)])
    assert validate(workflow, _needs(), artifacts) == []


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
    # Every registered suite runs on every engine (plan item 14).
    assert legs == {
        (suite, engine)
        for suite in SUITES
        for engine in ("chromium", "firefox", "webkit")
    }


@pytest.mark.parametrize("job", ["test", "python-coverage"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_python_shard_and_merge_jobs_are_required(
    tmp_path: Path,
    job: str,
    result: str,
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    failures = validate(_workflow(), _needs(**{job: {"result": result}}), artifacts)
    assert any(f"required job {job} did not succeed" in failure for failure in failures)


@pytest.mark.parametrize(
    "replacement",
    [
        "shard: [1, 2, 3]",
        "shard: [1, 2, 3, 3]",
        "shard: [1, 2, 3, 4, 5]",
        "shard: [1, 2, 3, 4]\n        exclude: [{shard: 4}]",
    ],
)
def test_rc_rejects_a_shrunken_or_duplicated_python_matrix(replacement: str) -> None:
    workflow = _workflow().replace("shard: [1, 2, 3, 4]", replacement)
    failures, _ = _binding_failures(workflow, set(SUITES))
    assert any("every Python shard exactly once" in failure for failure in failures)


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize("number", SHARDS)
def test_rc_requires_every_python_shard_artifact(
    tmp_path: Path,
    version: str,
    number: int,
) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    shard = artifacts / f"renewal-python-py{version}-shard-{number}"
    shard.rename(shard.with_name("absent"))
    failures = validate(_workflow(), _needs(), artifacts)
    assert any(
        f"Python {version}" in failure and "artifact" in failure for failure in failures
    )


def test_rc_requires_combined_coverage_evidence(tmp_path: Path) -> None:
    artifacts = _populate(tmp_path / "artifacts")
    (artifacts / "renewal-coverage-py3.13/report.json").unlink()
    failures = validate(_workflow(), _needs(), artifacts)
    assert any("Python 3.13" in failure for failure in failures)
