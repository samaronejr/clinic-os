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
    CatalogCounters,
    Counters,
    SerialEntry,
    ServerStateGuard,
    catalog_writes,
    load_manifest,
    read_counters,
)
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
        self.events: list[str] = []
        self.parallel = list(PARALLEL)
        self.serial = [SERIAL]
        self.guard: list[dict[str, object]] = []
        self.catalog_failures: list[str] = []

    def __call__(self, pytest_args: list[str], *, quiet: bool = False) -> int:
        phase = next(a for a in pytest_args if a.startswith("--ci-pytest-phase="))
        record = next(a for a in pytest_args if a.startswith("--ci-pytest-record="))
        name = phase.split("=", 1)[1]
        work = Path(record.split("=", 1)[1])
        self.calls[name] = pytest_args
        self.events.append(name)
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


class FakeWindow:
    def __init__(self, phases: FakePhases) -> None:
        self.phases = phases

    def check(self, work: Path) -> list[str]:
        self.phases.events.append("catalog-check")
        return self.phases.catalog_failures


@pytest.fixture
def phases(monkeypatch: pytest.MonkeyPatch) -> FakePhases:
    fake = FakePhases()

    @contextlib.contextmanager
    def databases(workers: int) -> Iterator[FakeWindow]:
        assert workers == 4
        fake.events.append("clones-created")
        try:
            yield FakeWindow(fake)
        finally:
            fake.events.append("clones-dropped")

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


def test_catalog_check_runs_after_the_parallel_phase(
    tmp_path: Path, phases: FakePhases
) -> None:
    assert _main(tmp_path) == 0
    assert phases.events[:4] == [
        "collect",
        "clones-created",
        "parallel",
        "catalog-check",
    ]


def test_clones_are_dropped_when_the_parallel_phase_raises(
    tmp_path: Path, phases: FakePhases, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(pytest_args: list[str], *, quiet: bool = False) -> int:
        if "--ci-pytest-phase=parallel" in pytest_args:
            raise KeyboardInterrupt
        return phases(pytest_args, quiet=quiet)

    monkeypatch.setattr(ci_pytest, "_run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _main(tmp_path)
    assert phases.events == ["collect", "clones-created", "clones-dropped"]


def test_catalog_check_failure_fails_the_run(
    tmp_path: Path, phases: FakePhases, capsys: pytest.CaptureFixture[str]
) -> None:
    phases.catalog_failures = ["cluster-global PostgreSQL state was written: x"]
    assert _main(tmp_path) == 1
    output = capsys.readouterr().out
    assert "FAIL cluster-global PostgreSQL state was written: x" in output
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
    guard = ServerStateGuard(tmp_path, "gw9")
    try:
        guard.before()
        assert guard.last is not None
        assert guard.last_counters is not None
        fingerprinted = set(guard.last)
        counted = set(guard.last_counters.catalogs)
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
    assert counted == shared - UNFINGERPRINTED_SHARED
    assert shared >= EXPECTED_SHARED | UNFINGERPRINTED_SHARED


def _counters(**catalogs: tuple[int, int, int, int, int]) -> Counters:
    server = ("on", "started", "loaded", "never")
    return Counters(
        server, {name: CatalogCounters(*values) for name, values in catalogs.items()}
    )


BASE = dict.fromkeys(sorted(EXPECTED_SHARED), (5, 5, 5, 5, 1))


@pytest.mark.parametrize(
    ("after", "server", "writes", "problems"),
    [
        # Rolled back: attempts counted, nothing committed.
        ({"pg_authid": (6, 5, 5, 5, 1)}, None, {}, []),
        # GRANT then REVOKE, both committed.
        (
            {"pg_auth_members": (6, 5, 6, 7, 1)},
            None,
            {
                "pg_auth_members": {
                    "inserted": 1,
                    "updated": 0,
                    "deleted": 1,
                    "committed": 2,
                }
            },
            [],
        ),
        # ANALYZE with no attempted write: nothing to hide.
        ({"pg_database": (5, 5, 5, 0, 2)}, None, {}, []),
        (
            {"pg_authid": (6, 5, 5, 0, 2)},
            None,
            {},
            [
                "pg_authid: ANALYZE reset the committed-write counter while 1 row "
                "writes were attempted"
            ],
        ),
        (
            {"pg_authid": (4, 5, 5, 5, 1)},
            None,
            {},
            ["pg_authid: the counters went backwards"],
        ),
        (
            {"pg_authid": (6, 5, 5, 7, 1)},
            None,
            {},
            ["pg_authid: the committed-write counter is inconsistent"],
        ),
        (
            {},
            ("off", "started", "loaded", "never"),
            {},
            ["track_counts is off, so writes are not counted"],
        ),
        (
            {},
            ("on", "restarted", "loaded", "never"),
            {},
            ["the PostgreSQL server restarted"],
        ),
        (
            {},
            ("on", "started", "reloaded", "never"),
            {},
            ["the server configuration was reloaded"],
        ),
        (
            {},
            ("on", "started", "loaded", "now"),
            {},
            ["the shared-object statistics were reset"],
        ),
    ],
    ids=[
        "rolled-back",
        "grant-then-revoke",
        "analyze-without-writes",
        "analyze-hides-writes",
        "backwards",
        "inconsistent",
        "track-counts-off",
        "restart",
        "reload",
        "stats-reset",
    ],
)
def test_catalog_writes_counts_committed_rows_and_fails_closed(
    after: dict[str, tuple[int, int, int, int, int]],
    server: tuple[str, ...] | None,
    writes: dict[str, dict[str, int]],
    problems: list[str],
) -> None:
    before = _counters(**BASE)
    current = _counters(**{**BASE, **after})
    if server is not None:
        current = Counters(server, current.catalogs)
    assert catalog_writes(before, current) == (writes, problems)


def test_catalog_writes_fails_closed_on_missing_catalogs() -> None:
    before = _counters(**BASE)
    reduced = {name: BASE[name] for name in BASE if name != "pg_auth_members"}
    assert catalog_writes(before, _counters(**reduced)) == (
        {},
        [
            "the shared catalogs' counters are incomplete",
            "the set of shared catalogs changed",
        ],
    )


def test_catalog_check_names_the_tests_around_the_first_counted_write(
    tmp_path: Path,
) -> None:
    windows = {
        "gw0": [
            ["tests/a.py::test_long_before", 0.0, 10.0],
            ["tests/a.py::test_near", 95.0, 99.0],
        ],
        "gw1": [
            ["tests/b.py::test_writer", 98.0, 101.0],
            ["tests/b.py::test_after", 150.0, 151.0],
        ],
    }
    for worker, entries in windows.items():
        (tmp_path / f"windows-{worker}.json").write_text(json.dumps(entries))
    counted = {
        "nodeid": "tests/b.py::test_writer",
        "worker": "gw1",
        "started": 98.0,
        "ended": 101.0,
        "writes": {"pg_auth_members": {"committed": 2}},
        "problems": [],
    }
    (tmp_path / "counted-gw1.jsonl").write_text(json.dumps(counted) + "\n")
    assert ci_pytest._suspects(tmp_path, "pg_auth_members") == (
        "first counted after tests/b.py::test_writer on gw1; the writer is "
        "normally among the tests running within 10 s before that, most recent "
        "first: ['tests/b.py::test_writer on gw1', 'tests/a.py::test_near on gw0']"
    )
    # Published only when the writer's backend exited: the phase's last tests.
    assert ci_pytest._suspects(tmp_path, "pg_authid") == (
        "counted only after the last test; the writer is normally among the "
        "tests running within 10 s before that, most recent first: "
        "['tests/b.py::test_after on gw1']"
    )


def test_catalog_check_fails_closed_while_a_phase_backend_is_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ci_pytest, "QUIESCE_SECONDS", 0.0)
    with _superuser() as admin:
        since = admin.execute("SELECT now()").fetchone()
        assert since is not None
        window = ci_pytest.CatalogWindow(admin, since[0])
        assert window.check(tmp_path) == [
            "shared-catalog counters have no baseline reading"
        ]
        window.baseline = read_counters(admin)
        with _superuser() as lingering:
            pid = lingering.info.backend_pid
            (failure,) = window.check(tmp_path)
    assert failure.startswith(
        "shared-catalog counters are unreadable: backends of the run are still "
        "connected after 0 s: "
    )
    assert f"({pid}, 'client backend', 'postgres', '')" in failure


def _alter_role_same_value(writer: psycopg.Connection[tuple[object, ...]]) -> None:
    row = writer.execute(
        "SELECT rolconnlimit FROM pg_roles WHERE rolname = 'clinic_app'"
    ).fetchone()
    assert row is not None
    writer.execute(
        sql.SQL("ALTER ROLE clinic_app CONNECTION LIMIT {}").format(sql.Literal(row[0]))
    )


def _grant_then_revoke(writer: psycopg.Connection[tuple[object, ...]]) -> None:
    # The fix-a11 review's shape (GRANT clinic_resolver TO clinic_app, then
    # REVOKE), on two synthetic NOLOGIN roles: each statement commits.
    member, granted = (
        sql.Identifier(f"synthetic_guard_{uuid4().hex}") for _ in range(2)
    )
    writer.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(member))
    writer.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(granted))
    writer.execute(sql.SQL("GRANT {} TO {}").format(granted, member))
    writer.execute(sql.SQL("REVOKE {} FROM {}").format(granted, member))
    writer.execute(sql.SQL("DROP ROLE {}, {}").format(member, granted))


@pytest.mark.parametrize(
    ("write", "fingerprinted", "counted"),
    [
        pytest.param(
            _alter_role_same_value,
            ["pg_authid"],
            {"pg_authid": {"inserted": 0, "updated": 1, "deleted": 0, "committed": 1}},
            id="alter-role-same-value",
        ),
        pytest.param(
            _grant_then_revoke,
            [],
            {
                "pg_auth_members": {
                    "inserted": 1,
                    "updated": 0,
                    "deleted": 1,
                    "committed": 2,
                },
                "pg_authid": {
                    "inserted": 2,
                    "updated": 0,
                    "deleted": 2,
                    "committed": 4,
                },
            },
            id="grant-then-revoke",
        ),
    ],
)
def test_guard_flags_a_value_preserving_cluster_global_write(
    tmp_path: Path,
    write: Callable[[psycopg.Connection[tuple[object, ...]]], None],
    fingerprinted: list[str],
    counted: dict[str, dict[str, int]],
) -> None:
    # Serial-classified: it commits cluster-global writes that end where
    # they began. The per-test fingerprint sees only a changed row version;
    # the cumulative counters see both, and the phase check names the test.
    guard = ServerStateGuard(tmp_path, "gw9")
    with _superuser() as admin:
        since = admin.execute("SELECT now()").fetchone()
        assert since is not None
        window = ci_pytest.CatalogWindow(admin, since[0])
        window.start()
        try:
            guard.before()
            with _superuser() as writer:
                write(writer)
                # Published now, not when this backend gets round to it.
                writer.execute("SELECT pg_stat_force_next_flush()")
            guard.after("synthetic::node")
        finally:
            guard.close()
        # Waits for the writer's and the guard's backends to exit.
        failures = window.check(tmp_path)
    violations = tmp_path / "guard-gw9.jsonl"
    records = (
        [json.loads(line) for line in violations.read_text().splitlines()]
        if violations.exists()
        else []
    )
    assert records == (
        [{"changed": fingerprinted, "nodeid": "synthetic::node", "worker": "gw9"}]
        if fingerprinted
        else []
    )
    (observed,) = [
        json.loads(line)
        for line in (tmp_path / "counted-gw9.jsonl").read_text().splitlines()
    ]
    # Only the catalogs this test wrote: an earlier test's late counts are
    # not this test's business.
    assert {name: observed["writes"][name] for name in counted} == counted
    assert observed["problems"] == []
    for name, counts in counted.items():
        (failure,) = [failure for failure in failures if f": {name} " in failure]
        assert failure.startswith(
            "cluster-global PostgreSQL state was written during the parallel "
            f"phase: {name} {counts} "
        )
        assert "first counted after synthetic::node on gw9" in failure
        assert "['synthetic::node on gw9']" in failure
    assert not [failure for failure in failures if "cannot certify" in failure]


@pytest.mark.django_db
def test_guard_ignores_database_local_and_rolled_back_work(
    tmp_path: Path, superuser_database_url: str
) -> None:
    guard = ServerStateGuard(tmp_path, "gw9")
    with _superuser() as admin:
        before = read_counters(admin)
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
                role = sql.Identifier(f"synthetic_guard_{uuid4().hex}")
                rolled_back.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
                rolled_back.execute(sql.SQL("GRANT clinic_app TO {}").format(role))
                rolled_back.rollback()
                rolled_back.autocommit = True
                rolled_back.execute("SELECT pg_stat_force_next_flush()")
            # Taken while the temp table and its pg_shdepend rows still exist.
            guard.after("synthetic::local")
    finally:
        guard.close()
    with _superuser() as admin:
        after = read_counters(admin)
    assert not (tmp_path / "guard-gw9.jsonl").exists()
    # The rolled-back writes were attempted and counted, but not committed.
    for name in ("pg_authid", "pg_auth_members"):
        assert after.catalogs[name].inserted > before.catalogs[name].inserted
    writes, problems = catalog_writes(before, after)
    assert "pg_authid" not in writes
    assert "pg_auth_members" not in writes
    assert problems == []


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
