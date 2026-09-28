"""Contract of the hosted Python job's parallel + serial driver (fix-a11)."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
import yaml
from coverage.config import read_coverage_config
from coverage.core import Core
from coverage.sysmon import SysMonitor
from ops.testing import ci_pytest
from ops.testing import renewal_runner as runner
from ops.testing.ci_pytest_plugin import (
    UNFINGERPRINTED_SHARED,
    SerialEntry,
    ServerStateGuard,
    load_manifest,
)
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PARALLEL = ("tests/a.py::test_one", "tests/a.py::test_two[x-1]")
SERIAL = "tests/b.py::test_server_write"
DRIVER = (
    "CLINIC_PDF_TOOLS=required uv run python -m ops.testing.ci_pytest --workers 4 "
    '--work-root "$RUNNER_TEMP/clinic-ci-pytest" '
    '--cov-xml "$RUNNER_TEMP/coverage-${{ matrix.python-version }}.xml"'
)
# Cross-check only: the guard derives its set from pg_class.relisshared.
EXPECTED_SHARED = {
    "pg_auth_members",
    "pg_authid",
    "pg_database",
    "pg_db_role_setting",
    "pg_parameter_acl",
    "pg_replication_origin",
    "pg_shdescription",
    "pg_shseclabel",
    "pg_subscription",
    "pg_tablespace",
}


def _manifest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ci-serial-tests.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_serial_manifest_grammar_is_closed(tmp_path: Path) -> None:
    valid = _manifest(
        tmp_path,
        "# comment\n\nserver-state tests/x/test_a.py::test_b\n"
        "worker-environment tests/x/test_c.py\n",
    )
    assert load_manifest(valid) == (
        SerialEntry("server-state", "tests/x/test_a.py::test_b"),
        SerialEntry("worker-environment", "tests/x/test_c.py"),
    )
    for invalid in (
        "slow tests/x/test_a.py::test_b\n",
        "server-state tests/x/test_a.py::test_b trailing\n",
        "server-state ../x/test_a.py\n",
        "server-state tests/x/test_a.py\nserver-state tests/x/test_a.py\n",
    ):
        with pytest.raises(pytest.UsageError):
            load_manifest(_manifest(tmp_path, invalid))


def test_serial_entry_matches_node_children_and_parametrizations() -> None:
    entry = SerialEntry("server-state", "tests/x/test_a.py::test_b")
    assert entry.matches("tests/x/test_a.py::test_b")
    assert entry.matches("tests/x/test_a.py::test_b[case-1]")
    assert not entry.matches("tests/x/test_a.py::test_b_more")
    assert SerialEntry("server-state", "tests/x/test_a.py").matches(
        "tests/x/test_a.py::Klass::test_b"
    )


def test_tracked_serial_classification_parses() -> None:
    entries = load_manifest()
    assert entries
    assert len({entry.prefix for entry in entries}) == len(entries)


class FakePhases:
    """Stand in for the pytest children; write the records the plugin writes."""

    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {}
        self.parallel = list(PARALLEL)
        self.serial = [SERIAL]
        self.guard: list[dict[str, object]] = []

    def __call__(self, pytest_args: list[str], *, quiet: bool = False) -> int:
        phase = next(a for a in pytest_args if a.startswith("--ci-pytest-phase="))
        record = next(a for a in pytest_args if a.startswith("--ci-pytest-record="))
        name = phase.split("=", 1)[1]
        work = Path(record.split("=", 1)[1])
        self.calls[name] = pytest_args
        if name == "collect":
            (work / "collected.json").write_text(
                json.dumps(
                    {"items": [*PARALLEL, SERIAL], "manifest": {SERIAL: [SERIAL]}}
                )
            )
            return 0
        executed = self.parallel if name == "parallel" else self.serial
        reports = {
            nodeid: [
                {"when": when, "outcome": "passed"}
                for when in ("setup", "call", "teardown")
            ]
            for nodeid in executed
        }
        (work / f"{name}-reports.json").write_text(json.dumps(reports))
        for violation in self.guard if name == "parallel" else []:
            with (work / "guard-gw0.jsonl").open("a") as handle:
                handle.write(json.dumps(violation) + "\n")
        return 0


@pytest.fixture
def phases(monkeypatch: pytest.MonkeyPatch) -> FakePhases:
    fake = FakePhases()

    @contextlib.contextmanager
    def databases(workers: int) -> Iterator[str]:
        assert workers == 4
        yield "test_clinic"

    monkeypatch.setattr(ci_pytest, "_run", fake)
    monkeypatch.setattr(ci_pytest, "_worker_databases", databases)
    return fake


def _main(tmp_path: Path, *extra: str) -> int:
    return ci_pytest.main(
        [
            "--workers",
            "4",
            "--work-root",
            str(tmp_path / "work"),
            "--cov-xml",
            str(tmp_path / "coverage.xml"),
            *extra,
        ]
    )


def test_driver_splits_one_population_and_keeps_the_coverage_contract(
    tmp_path: Path, phases: FakePhases
) -> None:
    assert _main(tmp_path) == 0
    targets = list(ci_pytest.coverage_targets())
    assert targets
    assert targets == [
        line
        for line in (PROJECT_ROOT / "ops/testing/coverage-targets.txt")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    ]
    collect = phases.calls["collect"]
    assert collect[-2:] == ["--collect-only", "--quiet", "--quiet", "tests"][-2:]
    assert "--collect-only" in collect
    parallel = phases.calls["parallel"]
    assert parallel[-1] == "tests"
    assert {"--numprocesses=4", "--dist=loadfile", "--ci-pytest-guard"} <= set(parallel)
    assert "--reuse-db" in parallel
    assert "--cov-report=" in parallel
    assert "--cov-fail-under=90" not in parallel
    serial = phases.calls["serial"]
    assert serial[-1] == "tests/b.py"
    assert "--ci-pytest-guard" not in serial
    assert {
        "--reuse-db",
        "--cov-append",
        "--cov-report=term-missing",
        f"--cov-report=xml:{tmp_path / 'coverage.xml'}",
        "--cov-fail-under=90",
    } <= set(serial)
    for phase in (parallel, serial):
        assert [arg for arg in phase if arg.startswith("--cov=")] == targets


def test_driver_passes_extra_pytest_arguments_to_both_phases(
    tmp_path: Path, phases: FakePhases
) -> None:
    assert _main(tmp_path, "--", "-q") == 0
    assert "-q" in phases.calls["parallel"]
    assert "-q" in phases.calls["serial"]
    assert "-q" not in phases.calls["collect"]


def test_pytest_phases_measure_with_the_sysmon_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[list[str], dict[str, str]]] = []

    def run_command(
        argv: list[str], overrides: dict[str, str], *, quiet: bool = False
    ) -> int:
        captured.append((argv, overrides))
        return 0

    monkeypatch.setattr(ci_pytest, "_run_command", run_command)
    assert ci_pytest._run(["--collect-only"]) == 0
    assert captured == [
        (
            [sys.executable, "-m", "pytest", "--collect-only"],
            {"COVERAGE_CORE": "sysmon"},
        )
    ]
    # The project's coverage configuration keeps sysmon usable: coverage.py
    # would warn and fall back to the C tracer (branch coverage before 3.14,
    # dynamic contexts, gevent-style concurrency).
    monkeypatch.setenv("COVERAGE_CORE", "sysmon")
    warnings: list[str] = []

    def warn(msg: str, *_: object, **__: object) -> None:
        warnings.append(msg)

    config = read_coverage_config(
        config_file=str(PROJECT_ROOT / "pyproject.toml"), warn=warn
    )
    core = Core(
        warn=warn,
        debug=None,
        config=config,
        dynamic_contexts=bool(config.dynamic_context),
    )
    assert core.tracer_class is SysMonitor
    assert warnings == []


@pytest.mark.parametrize(
    "case",
    [
        (PARALLEL[:1], [SERIAL], "parallel phase did not run 1"),
        (PARALLEL, [], "serial phase did not run 1"),
        ([*PARALLEL, SERIAL], [SERIAL], "parallel phase ran 1 unexpected"),
        ([*PARALLEL, "tests/c.py::test_new"], [SERIAL], "ran 1 unexpected"),
    ],
)
def test_population_check_fails_on_a_dropped_or_misrouted_test(
    tmp_path: Path,
    phases: FakePhases,
    capsys: pytest.CaptureFixture[str],
    case: tuple[list[str], list[str], str],
) -> None:
    phases.parallel, phases.serial, message = case
    assert _main(tmp_path) == 1
    output = capsys.readouterr().out
    assert message in output
    assert "checks=FAILED" in output


def test_guard_violation_fails_the_run(
    tmp_path: Path, phases: FakePhases, capsys: pytest.CaptureFixture[str]
) -> None:
    phases.guard = [{"nodeid": PARALLEL[0], "worker": "gw0", "changed": ["pg_authid"]}]
    assert _main(tmp_path) == 1
    output = capsys.readouterr().out
    assert f"state changed during {PARALLEL[0]} on gw0" in output


def _superuser() -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname="postgres", autocommit=True
    )


def test_guard_fingerprints_every_shared_catalog_but_pg_shdepend(
    tmp_path: Path,
) -> None:
    guard = ServerStateGuard(tmp_path / "guard.jsonl")
    try:
        guard.before()
        assert guard.last is not None
        fingerprinted = set(guard.last)
    finally:
        guard.close()
    with _superuser() as admin:
        shared = {
            str(row[0])
            for row in admin.execute(
                "SELECT relname FROM pg_class WHERE relisshared AND relkind = 'r'"
            ).fetchall()
        }
    assert set(UNFINGERPRINTED_SHARED) == {"pg_shdepend"}
    assert fingerprinted == (shared - UNFINGERPRINTED_SHARED) | {"pg_file_settings"}
    assert shared >= EXPECTED_SHARED | UNFINGERPRINTED_SHARED


def test_guard_flags_a_value_preserving_cluster_global_write(tmp_path: Path) -> None:
    # Serial-classified: it commits a (value-identical) ALTER ROLE.
    log = tmp_path / "guard.jsonl"
    guard = ServerStateGuard(log)
    try:
        guard.before()
        with _superuser() as admin:
            row = admin.execute(
                "SELECT rolconnlimit FROM pg_roles WHERE rolname = 'clinic_app'"
            ).fetchone()
            assert row is not None
            admin.execute(
                sql.SQL("ALTER ROLE clinic_app CONNECTION LIMIT {}").format(
                    sql.Literal(row[0])
                )
            )
        guard.after("synthetic::node", "gw9")
    finally:
        guard.close()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records == [
        {"nodeid": "synthetic::node", "worker": "gw9", "changed": ["pg_authid"]}
    ]


@pytest.mark.django_db
def test_guard_ignores_database_local_and_rolled_back_work(
    tmp_path: Path, superuser_database_url: str
) -> None:
    log = tmp_path / "guard.jsonl"
    guard = ServerStateGuard(log)
    try:
        guard.before()
        with psycopg.connect(superuser_database_url, autocommit=True) as local:
            local.execute("CREATE TEMP TABLE synthetic_guard_probe (value integer)")
            local.execute("GRANT SELECT ON synthetic_guard_probe TO clinic_app")
            # The grant is recorded in the unfingerprinted pg_shdepend.
            depends = local.execute(
                "SELECT count(*) FROM pg_shdepend "
                "WHERE objid = 'synthetic_guard_probe'::regclass"
            ).fetchone()
            assert depends is not None
            assert depends[0] > 0
            with psycopg.connect(superuser_database_url) as rolled_back:
                rolled_back.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN").format(
                        sql.Identifier(f"synthetic_guard_{uuid4().hex}")
                    )
                )
                rolled_back.rollback()
            # Taken while the temp table and its pg_shdepend rows still exist.
            guard.after("synthetic::local", "gw9")
    finally:
        guard.close()
    assert not log.exists()


def test_hosted_job_make_ci_and_runner_run_the_shared_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Run claimed database quality gates"
    )
    lines = [line.strip() for line in script.splitlines()]
    assert DRIVER in lines
    assert not [line for line in lines if "run pytest" in line or "-m pytest" in line]
    artifact = next(
        step
        for step in steps
        if step.get("name") == "Publish machine-readable coverage"
    )
    assert artifact["with"]["name"] == "renewal-coverage-py${{ matrix.python-version }}"
    assert (
        artifact["with"]["path"]
        == "${{ runner.temp }}/coverage-${{ matrix.python-version }}.xml"
    )
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "CI_PYTEST_WORKERS ?= 4" in makefile.splitlines()
    recipe = makefile.split("\nci:\n", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
    assert '-m ops.testing.ci_pytest --workers "$(CI_PYTEST_WORKERS)"' in recipe
    assert "run pytest" not in recipe
    assert "-m pytest" not in recipe
    captured: list[list[str]] = []

    @contextlib.contextmanager
    def provision(
        repository: Path, token: str, *, docker: object = None
    ) -> Iterator[object]:
        yield runner.ProvisionedDatabase(
            app_dsn="postgresql://clinic_app:pw@127.0.0.1:55432/clinic",
            app_password="pw",  # noqa: S106 - synthetic fixture value.
            container="clinic_renewal_db_drv",
            database="clinic",
            owner_dsn="postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
            owner_password="pw",  # noqa: S106 - synthetic fixture value.
            port=55432,
            postgres_password="pw",  # noqa: S106 - synthetic fixture value.
            super_dsn="postgresql://clinic_super:pw@127.0.0.1:55432/clinic",
            super_password="pw",  # noqa: S106 - synthetic fixture value.
            volume="clinic_renewal_db_drv_data",
        )

    def run(
        argv: list[str], environment: dict[str, str], log: Path, **_: object
    ) -> int:
        captured.append(argv)
        return 0

    monkeypatch.setattr(runner, "_provision_database", provision)
    monkeypatch.setattr(runner, "_create_test_database", lambda *args: None)
    monkeypatch.setattr(runner, "_migrate", lambda *args: None)
    monkeypatch.setattr(runner, "_run_bounded", run)
    assert runner._gate_coverage(PROJECT_ROOT, tmp_path, "drv") == [
        {"command": "coverage", "exit": 0}
    ]
    assert captured == [
        [
            captured[0][0],
            "-m",
            "ops.testing.ci_pytest",
            "--workers=4",
            f"--work-root={tmp_path / 'ci-pytest'}",
            "--",
            "-q",
        ]
    ]
    assert tuple(runner.COVERAGE_TARGETS) == ci_pytest.coverage_targets()
