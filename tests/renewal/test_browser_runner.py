from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing import renewal_runner as runner
from ops.testing.browser_server_supervisor import SupervisedMaster
from ops.testing.isolation_common import IsolationError, JsonObject
from playwright.sync_api import sync_playwright

from renewal.browser import a11y_support, engines

if TYPE_CHECKING:
    from collections.abc import Callable

REPOSITORY = Path(__file__).resolve().parents[2]


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    return path


def test_suite_registry_is_closed_and_registered_files_exist() -> None:
    assert set(runner.SUITES) == {
        "agenda",
        "amendments",
        "attachments",
        "availability",
        "billing",
        "clinic-settings",
        "contacts",
        "consent",
        "encounter",
        "end-to-end",
        "clinical-history",
        "clinician-video",
        "video-recovery",
        "locale",
        "patient-access",
        "patient-video",
        "questionnaires",
        "retention",
        "self-booking",
        "waitlist",
        "reminders",
        "teleconsult",
        "primitives",
        "prescription-draft",
        "prescribing",
        "document-verification",
        "smoke",
        "staff-intake",
        "workspace",
    }
    assert "prescription-draft" in runner.FIXTURE_SUITES
    assert runner.SIGNING_SUITES <= runner.FIXTURE_SUITES
    assert "--cov=apps.prescription" in runner.COVERAGE_TARGETS
    assert set(runner.SUITES) >= runner.FIXTURE_SUITES
    assert "video-recovery" in runner.FIXTURE_SUITES & runner.VIDEO_SUITES
    # The rehearsal drives every synthetic adapter slice in one runtime.
    assert "end-to-end" in (
        runner.FIXTURE_SUITES
        & runner.VIDEO_SUITES
        & runner.SIGNING_SUITES
        & runner.PAYMENT_SUITES
    )
    for suite in runner.SUITES.values():
        for relpath in suite:
            assert (REPOSITORY / relpath).is_file()
            assert relpath.startswith("tests/renewal/browser/")


def test_coverage_targets_include_the_teleconsult_domain() -> None:
    assert "--cov=apps.teleconsult" in runner.COVERAGE_TARGETS


def test_ci_gate_names_are_the_plan_gates() -> None:
    assert runner.CI_GATES == (
        "static",
        "migration",
        "coverage",
        "dependency",
        "image-tls",
        "browser",
    )


def test_main_rejects_invalid_grammar() -> None:
    assert runner.main([]) == 2
    assert runner.main(["bogus"]) == 2
    assert runner.main(["browser"]) == 2
    assert runner.main(["browser", "--suite"]) == 2
    assert runner.main(["browser", "--suite", "smoke", "--bogus", "x"]) == 2
    assert runner.main(["ci", "--suite", "smoke"]) == 2


def test_main_rejects_unknown_suite(tmp_path: Path) -> None:
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "runtime-https",
                "--artifact-root",
                str(_private(tmp_path / "artifacts")),
            ]
        )
        == 2
    )


def test_serving_dsn_requires_exactly_the_app_role() -> None:
    good = "postgresql://clinic_app:pw@127.0.0.1:55432/clinic"
    assert runner._validate_serving_dsn(good) == good
    for bad in (
        "postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
        "postgresql://postgres:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_super:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_resolver:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_app:pw@db.internal:55432/clinic",
        "postgresql://clinic_app:pw@10.0.0.4:55432/clinic",
        "postgresql://clinic_app@127.0.0.1:55432/clinic",
        "postgresql://clinic_app:pw@127.0.0.1:55432/",
        "postgresql://clinic_app:pw@127.0.0.1/clinic",
        "postgres://clinic_app:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_app:pw@127.0.0.1:5432/clinic",
        "not-a-dsn",
        "",
    ):
        with pytest.raises(IsolationError):
            runner._validate_serving_dsn(bad)


def test_owner_dsn_override_is_rejected_before_any_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLINIC_RENEWAL_APP_DATABASE_URL",
        "postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
    )

    def _explode(*args: object, **kwargs: object) -> None:
        message = "docker must never run for a rejected DSN"
        raise AssertionError(message)

    monkeypatch.setattr(runner, "_docker", _explode)
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
        == 2
    )


def test_missing_browser_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", "/nonexistent/chrome")
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
        == 2
    )


def test_browser_resolution_honors_the_private_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", str(executable))
    assert runner._resolve_browser() == str(executable)
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", str(tmp_path / "gone"))
    with pytest.raises(IsolationError, match="browser"):
        runner._resolve_browser()
    monkeypatch.delenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE")
    monkeypatch.setattr("ops.testing.renewal_runner.shutil.which", lambda _name: None)
    with pytest.raises(IsolationError, match="browser"):
        runner._resolve_browser()


def test_engine_selection_defaults_to_chromium_and_rejects_the_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    assert runner.ENGINES == ("chromium", "firefox", "webkit")
    assert runner._resolve_engine(None) == "chromium"
    assert runner._resolve_engine("webkit") == "webkit"
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "firefox")
    assert runner._resolve_engine(None) == "firefox"
    assert runner._resolve_engine("firefox") == "firefox"
    with pytest.raises(IsolationError, match="different engines"):
        runner._resolve_engine("webkit")
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "netscape")
    with pytest.raises(IsolationError, match="not supported: netscape"):
        runner._resolve_engine(None)
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE")
    with pytest.raises(IsolationError, match="not supported: Firefox"):
        runner._resolve_engine("Firefox")


PORTED_SUITES = (
    "billing",
    "clinic-settings",
    "clinical-history",
    "clinician-video",
    "consent",
    "end-to-end",
    "patient-video",
    "video-recovery",
)
# Engine-specific Playwright/browser APIs live only in engines.py; a suite
# that uses one directly would crash or silently fall back to Chromium.
ENGINE_SPECIFIC_APIS = (
    "driver.chromium",
    ".chromium.launch",
    "new_cdp_session",
    "chrome://",
    "--use-fake-device",
    "grant_permissions(",
    "clipboard.readText",
    "CLINIC_RENEWAL_BROWSER_EXECUTABLE",
    "wait_for_function(",
)


def test_browser_suites_leave_engine_specific_apis_to_the_engines_module() -> None:
    offenders = [
        (relpath, api)
        for paths in runner.SUITES.values()
        for relpath in paths
        for api in ENGINE_SPECIFIC_APIS
        if api in (REPOSITORY / relpath).read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize("engine", ["firefox", "webkit"])
@pytest.mark.parametrize("suite", PORTED_SUITES)
def test_every_suite_runs_on_every_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suite: str,
    engine: str,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    started: list[tuple[str, str]] = []

    def fake_suite(*args: object) -> JsonObject:
        started.append((str(args[1]), str(args[5])))
        return {"suite": str(args[1]), "engine": str(args[5])}

    monkeypatch.setattr(runner, "_run_browser_suite", fake_suite)
    artifacts = _private(tmp_path / "artifacts")
    arguments = ["browser", "--suite", suite, "--engine", engine]
    assert runner.main([*arguments, "--artifact-root", str(artifacts)]) == 0
    assert started == [(suite, engine)]


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["--engine", "netscape"], None),
        ([], "netscape"),
        (["--engine", "firefox"], "webkit"),
        (["--engine", ""], None),
    ],
)
def test_main_rejects_bad_engines_before_any_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    environment: str | None,
) -> None:
    if environment is None:
        monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    else:
        monkeypatch.setenv("CLINIC_BROWSER_ENGINE", environment)

    def _explode(*args: object, **kwargs: object) -> None:
        message = "a rejected engine must never provision or capture"
        raise AssertionError(message)

    monkeypatch.setattr(runner, "_docker", _explode)
    monkeypatch.setattr(runner, "_capture_source", _explode)
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                *arguments,
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
        == 2
    )


def test_unavailable_engine_fails_the_suite_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    monkeypatch.delenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        runner, "_managed_executable", lambda _engine: str(tmp_path / "absent")
    )

    def _explode(*args: object, **kwargs: object) -> None:
        message = "an unavailable engine must never provision"
        raise AssertionError(message)

    monkeypatch.setattr(runner, "_docker", _explode)
    monkeypatch.setattr(runner, "_capture_source", _explode)
    artifacts = tmp_path / "artifacts"
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--engine",
                "webkit",
                "--artifact-root",
                str(artifacts),
            ]
        )
        == 2
    )
    assert not (artifacts / "report.json").exists()


def test_managed_engine_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "firefox"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    requested: list[str] = []

    def managed(engine: str) -> str:
        requested.append(engine)
        return str(executable)

    monkeypatch.delenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(runner, "_managed_executable", managed)
    assert runner._resolve_browser("firefox") == str(executable)
    assert requested == ["firefox"]
    executable.chmod(0o644)
    with pytest.raises(IsolationError, match="playwright install firefox"):
        runner._resolve_browser("firefox")
    executable.chmod(0o755)
    # The Chromium override never stands in for another engine.
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", str(executable))
    with pytest.raises(IsolationError, match="Chromium-only"):
        runner._resolve_browser("webkit")
    assert requested == ["firefox", "firefox"]


@pytest.mark.parametrize("engine", ["firefox", "webkit"])
def test_managed_executable_names_the_playwright_engine_build(engine: str) -> None:
    path = Path(runner._managed_executable(engine))
    assert path.is_absolute()
    assert f"{engine}-" in str(path)


def test_ci_browser_gate_always_runs_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "firefox")
    engines: dict[str, object] = {}

    def fake_suite(*args: object) -> JsonObject:
        engines[str(args[1])] = args[5]
        return {"suite": str(args[1])}

    monkeypatch.setattr(runner, "_run_browser_suite", fake_suite)
    results = runner._gate_browser(REPOSITORY, tmp_path, tmp_path / "run")
    assert {result["exit"] for result in results} == {0}
    assert engines == dict.fromkeys(runner.SUITES, "chromium")


def test_axe_support_blocks_only_serious_and_critical_impacts() -> None:
    violations: list[dict[str, object]] = [
        {"id": "image-alt", "impact": "critical"},
        {"id": "color-contrast", "impact": "serious"},
        {"id": "region", "impact": "moderate"},
        {"id": "heading-order", "impact": "minor"},
    ]
    assert [v["id"] for v in a11y_support.blocking(violations)] == [
        "image-alt",
        "color-contrast",
    ]
    assert a11y_support.AXE_URL == "/static/vendor/axe/axe.min.js"
    assert (
        REPOSITORY / "static" / a11y_support.AXE_URL.removeprefix("/static/")
    ).is_file()


def test_suite_side_engine_selection_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    assert engines.selected_engine() == "chromium"
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "webkit")
    assert engines.selected_engine() == "webkit"
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "netscape")
    with pytest.raises(pytest.fail.Exception, match="netscape"):
        engines.selected_engine()


def test_mobile_profiles_are_the_touch_phones_of_the_device_matrix() -> None:
    assert {p.label for p in engines.MOBILE_PROFILES.values()} == {
        "iPhone 15",
        "Pixel 8",
    }
    assert engines.ENGINES == runner.ENGINES
    assert engines.DEFAULT_ENGINE == runner.DEFAULT_ENGINE
    # The static profiles mirror the pinned Playwright device descriptors.
    with sync_playwright() as driver:
        for profile in engines.MOBILE_PROFILES.values():
            descriptor = driver.devices[profile.label]
            assert descriptor["viewport"] == {
                "width": profile.width,
                "height": profile.height,
            }
            assert descriptor["device_scale_factor"] == profile.device_scale_factor
            assert descriptor["user_agent"] == profile.user_agent
            assert descriptor["is_mobile"] is True
            assert descriptor["has_touch"] is True


def test_artifact_root_must_be_excluded_or_external(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "ensure_private_directory", lambda _path: True)
    inside = REPOSITORY / "renewal-artifacts"
    with pytest.raises(IsolationError, match="artifact root"):
        runner._artifact_root(str(inside), REPOSITORY)
    excluded = REPOSITORY / ".omo" / "renewal-runs" / "x"
    assert runner._artifact_root(str(excluded), REPOSITORY) == excluded
    outside = tmp_path / "artifacts"
    assert runner._artifact_root(str(outside), REPOSITORY) == outside
    with pytest.raises(IsolationError):
        runner._artifact_root("relative/path", REPOSITORY)


def test_artifact_root_rejects_symlink(tmp_path: Path) -> None:
    target = _private(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(IsolationError):
        runner._artifact_root(str(link), REPOSITORY)


def test_junit_verdict_rejects_zero_tests_failures_errors_and_skips(
    tmp_path: Path,
) -> None:
    def _write(name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(body)
        return path

    ok = _write(
        "ok.xml",
        '<testsuite tests="3" failures="0" errors="0" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(ok) == (3, 0, 0, 0)

    zero = _write(
        "zero.xml",
        '<testsuite tests="0" failures="0" errors="0" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(zero)[0] == 0

    failed = _write(
        "failed.xml",
        '<testsuite tests="2" failures="1" errors="0" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(failed)[1] == 1

    errored = _write(
        "errored.xml",
        '<testsuite tests="2" failures="0" errors="1" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(errored)[2] == 1

    skipped = _write(
        "skipped.xml",
        '<testsuite tests="2" failures="0" errors="0" skipped="2"></testsuite>',
    )
    assert runner._junit_verdict(skipped)[3] == 2

    with pytest.raises(IsolationError):
        runner._junit_verdict(tmp_path / "absent.xml")
    garbage = _write("garbage.xml", "not xml at all")
    with pytest.raises(IsolationError):
        runner._junit_verdict(garbage)


def test_pytest_environment_exports_only_the_private_fixture_inputs(
    tmp_path: Path,
) -> None:
    environment = runner._pytest_environment(
        base_url="http://127.0.0.1:48000",
        artifact_root=tmp_path,
        browser="/usr/bin/google-chrome",
        engine="webkit",
        username="renewal-owner-x",
        password="secret-password",  # noqa: S106 - synthetic fixture value.
    )
    assert environment["CLINIC_BROWSER_ENGINE"] == "webkit"
    assert environment["CLINIC_RENEWAL_BASE_URL"] == "http://127.0.0.1:48000"
    assert environment["CLINIC_RENEWAL_ARTIFACT_ROOT"] == str(tmp_path)
    assert environment["CLINIC_RENEWAL_BROWSER_EXECUTABLE"] == "/usr/bin/google-chrome"
    assert environment["CLINIC_RENEWAL_USERNAME"] == "renewal-owner-x"
    assert environment["CLINIC_RENEWAL_PASSWORD"] == "secret-password"  # noqa: S105
    for leaked in (
        "APP_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "TEST_SUPERUSER_DATABASE_URL",
        "CLINIC_OWNER_PASSWORD",
        "CLINIC_APP_PASSWORD",
        "POSTGRES_PASSWORD",
    ):
        assert leaked not in environment


def test_bounded_subprocess_kills_a_hung_child_group(tmp_path: Path) -> None:
    log = tmp_path / "hung.log"
    started = time.monotonic()
    code = runner._run_bounded(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        {},
        log,
        timeout=2,
    )
    assert code != 0
    assert time.monotonic() - started < 30
    assert log.read_text(errors="replace") == ""


def test_provisioned_database_uses_unique_names_and_cleans_only_its_own(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] == ("container", "inspect"):
            return "abc123\n"
        if arguments[:2] == ("exec",) or arguments[0] == "exec":
            return ""
        return ""

    with runner._provision_database(REPOSITORY, "deadbeef", docker=fake_docker) as db:
        assert db.container.startswith("clinic_renewal_db_deadbeef")
        assert db.volume.startswith("clinic_renewal_db_deadbeef")
        assert db.app_dsn.startswith("postgresql://clinic_app:")
        assert db.owner_dsn.startswith("postgresql://clinic_owner:")
        assert db.super_dsn.startswith("postgresql://clinic_super:")
        assert f"127.0.0.1:{db.port}" in db.app_dsn
    removed = [call for call in calls if call[:2] == ("container", "rm")]
    volumes = [call for call in calls if call[:2] == ("volume", "rm")]
    assert removed == [("container", "rm", "-f", db.container)]
    assert volumes == [("volume", "rm", db.volume)]
    assert all("app_mei" not in " ".join(call) for call in calls)


def test_provision_cleans_up_when_readiness_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[0] == "exec":
            message = "database never became ready"
            raise IsolationError(message)
        return ""

    with (
        pytest.raises(IsolationError, match="ready"),
        runner._provision_database(REPOSITORY, "cafef00d", docker=fake_docker),
    ):
        pass
    assert ("container", "rm", "-f", "clinic_renewal_db_cafef00d") in calls
    assert ("volume", "rm", "clinic_renewal_db_cafef00d_data") in calls


def test_provision_teardown_reports_docker_failures() -> None:
    """Removal failures at the Docker boundary must surface, not vanish."""
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] in {("container", "rm"), ("volume", "rm")}:
            message = "repro: Docker removal failed"
            raise runner.RenewalRunnerError(message)
        return ""

    with (
        pytest.raises(IsolationError, match="teardown failed"),
        runner._provision_database(REPOSITORY, "badteard", docker=fake_docker),
    ):
        pass
    assert ("container", "rm", "-f", "clinic_renewal_db_badteard") in calls
    assert ("volume", "rm", "clinic_renewal_db_badteard_data") in calls


def test_provision_teardown_failure_surfaces_over_body_error() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] == ("volume", "rm"):
            message = "repro: volume removal timed out"
            raise runner.RenewalRunnerError(message)
        return ""

    body_error = ValueError("body exploded")
    with (
        pytest.raises(IsolationError, match="teardown failed") as caught,
        runner._provision_database(REPOSITORY, "bodyfail", docker=fake_docker),
    ):
        raise body_error
    assert caught.value.__context__ is body_error
    assert ("container", "rm", "-f", "clinic_renewal_db_bodyfail") in calls
    assert ("volume", "rm", "clinic_renewal_db_bodyfail_data") in calls


def test_provision_teardown_completes_through_a_second_interrupt() -> None:
    """A SIGTERM inside teardown still attempts every remaining removal."""
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] == ("container", "rm"):
            os.kill(os.getpid(), signal.SIGTERM)
        return ""

    previous = signal.signal(signal.SIGTERM, runner._interrupted)
    try:
        with (
            pytest.raises(runner.RenewalInterruptError),
            runner._provision_database(REPOSITORY, "sigterm2", docker=fake_docker),
        ):
            pass
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert ("container", "rm", "-f", "clinic_renewal_db_sigterm2") in calls
    assert ("volume", "rm", "clinic_renewal_db_sigterm2_data") in calls


def test_provision_teardown_tolerates_absent_resources() -> None:
    """A resource the daemon reports as absent is already removed."""

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        if arguments[:2] == ("container", "rm"):
            message = "renewal docker command failed: No such container"
            raise runner.RenewalRunnerError(message)
        if arguments[:2] == ("volume", "rm"):
            message = "renewal docker command failed: no such volume"
            raise runner.RenewalRunnerError(message)
        return ""

    with runner._provision_database(REPOSITORY, "gonegone", docker=fake_docker):
        pass


def test_await_database_propagates_interruption() -> None:
    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        message = "renewal runner interrupted"
        raise runner.RenewalInterruptError(message)

    with pytest.raises(runner.RenewalInterruptError):
        runner._await_database("clinic_renewal_db_x", docker=fake_docker)


def test_terminate_master_retries_once_then_propagates_the_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful retry still propagates the observed cancellation."""
    master = object.__new__(SupervisedMaster)
    terminations: list[str] = []

    def flaky_terminate() -> None:
        terminations.append("terminate")
        if len(terminations) == 1:
            message = "renewal runner interrupted"
            raise runner.RenewalInterruptError(message)

    monkeypatch.setattr(master, "terminate", flaky_terminate)
    with pytest.raises(runner.RenewalInterruptError):
        runner._terminate_master(master)
    assert terminations == ["terminate", "terminate"]

    def stuck_terminate() -> None:
        message = "renewal runner interrupted"
        raise runner.RenewalInterruptError(message)

    monkeypatch.setattr(master, "terminate", stuck_terminate)
    with pytest.raises(runner.RenewalInterruptError):
        runner._terminate_master(master)


def test_teardown_interrupt_is_reaped_then_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first SIGTERM during otherwise-successful teardown still exits 130.

    The child is real: the interrupted first ``terminate`` is retried so the
    bounded TERM/reap completes, then the cancellation propagates instead of
    becoming a successful run.
    """
    log = tmp_path / "teardown.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    master = SupervisedMaster(process)
    original = SupervisedMaster.terminate
    calls = 0

    def terminate_with_signal(self: SupervisedMaster) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        original(self)

    previous = signal.signal(signal.SIGTERM, runner._interrupted)
    monkeypatch.setattr(SupervisedMaster, "terminate", terminate_with_signal)
    try:
        with pytest.raises(runner.RenewalInterruptError):
            runner._terminate_master(master)
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process.poll() is None:
            original(master)
    assert calls == 2
    assert process.poll() is not None


def test_ci_interruption_stops_later_gates_and_exits_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real SIGTERM inside a gate must stop scheduling and exit 130."""
    calls: list[str] = []

    def interrupt(*args: object) -> list[JsonObject]:
        calls.append("static")
        os.kill(os.getpid(), signal.SIGTERM)
        return []

    def gate(name: str) -> Callable[..., list[JsonObject]]:
        def run(*args: object) -> list[JsonObject]:
            calls.append(name)
            return [{"command": name, "exit": 0}]

        return run

    monkeypatch.setattr(runner, "_gate_static", interrupt)
    for name in runner.CI_GATES[1:]:
        monkeypatch.setattr(runner, f"_gate_{name.replace('-', '_')}", gate(name))
    artifact_root = _private(tmp_path / "artifacts")
    assert runner.main(["ci", "--artifact-root", str(artifact_root)]) == 130
    assert calls == ["static"]
    report = json.loads((artifact_root / "ci-report.json").read_text())
    assert report["ok"] is False
    assert report["gates"] == {
        "static": [
            {"command": "static", "exit": 130, "error": "renewal runner interrupted"}
        ]
    }


def test_gate_browser_propagates_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted_suite(*args: object) -> JsonObject:
        message = "renewal runner interrupted"
        raise runner.RenewalInterruptError(message)

    monkeypatch.setattr(runner, "_run_browser_suite", interrupted_suite)
    with pytest.raises(runner.RenewalInterruptError):
        runner._gate_browser(REPOSITORY, tmp_path, tmp_path / "run")


def test_stale_or_missing_record_is_rejected(tmp_path: Path) -> None:
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--artifact-root",
                str(tmp_path / "artifacts"),
                "--record",
                str(tmp_path / "absent-record.json"),
            ]
        )
        == 2
    )


def test_runner_module_never_shells_out() -> None:
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "/bin/sh" not in source


def test_renewal_settings_reject_an_owner_role_dsn(tmp_path: Path) -> None:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "DJANGO_SETTINGS_MODULE": "config.settings.renewal",
        "APP_DATABASE_URL": "postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
        "SECRET_KEY": "renewal-test-secret",
        "PYTHONPATH": str(REPOSITORY),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        check=False,
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode != 0
    environment["APP_DATABASE_URL"] = (
        "postgresql://clinic_app:pw@127.0.0.1:55432/clinic"
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        check=False,
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode()[-500:]


def test_excluded_predicate_matches_the_snapshot_contract() -> None:
    for path in (
        ".omo/evidence/x.json",
        ".venv/bin/python",
        "__pycache__/x.pyc",
        "node_modules/pkg/index.js",
        "evidence/run/out.txt",
        "a/.env",
        "a/.env.local",
        "a/credentials.json",
        "a/id_rsa",
        "a/cert.pem",
        "a/key.key",
        "a/bundle.p12",
        "a/bundle.pfx",
        "a/.env.prod",
        "a/.mypy_cache/x",
        "a/.pytest_cache/x",
        "a/.ruff_cache/x",
    ):
        assert runner._excluded(path) is True, path
    for path in (
        "ops/testing/renewal_runner.py",
        "tests/renewal/browser/test_smoke.py",
        "docs/guide.md",
        ".env.example",
        "README.md",
    ):
        assert runner._excluded(path) is False, path
