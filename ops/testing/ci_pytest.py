"""Run the hosted Python test job's whole default collection in two phases.

One command shared by the hosted workflow, ``make ci`` and the renewal
runner's coverage gate::

    python -m ops.testing.ci_pytest --workers 4 --work-root DIR [--cov-xml PATH]

1. ``collect``: an independent ``pytest --collect-only tests`` records the
   reference population (the node IDs the single-process form would run) and
   resolves ``ci-serial-tests.txt``; every classification must match a test.
2. Worker databases: the owner-owned test database (``test_<db>``, created by
   ``make db-bootstrap`` or the runner) is migrated once, then cloned per
   xdist worker as ``test_<db>_gw<N>`` (pytest-django's xdist suffix) with the
   template's owner, encoding and database ACL. ``clinic_owner`` is NOCREATEDB,
   so the superuser clones.
3. ``parallel``: ``pytest -n N --dist loadfile`` over everything not
   classified serial, with the per-test server-state guard on, between two
   readings of the shared catalogs' cumulative ``pg_stat`` counters.
   Afterwards the clones are dropped (also on any failure), before the serial
   phase: every ``DROP DATABASE`` forces a checkpoint, and live clones would
   make each of the serial phase's own drops flush their files.
4. ``serial``: one process runs the classified tests alone, appends to the
   same coverage data and applies the unchanged reports and
   ``--cov-fail-under=90`` to the combined total.
5. Checks: parallel executed + serial executed == reference, disjointly, each
   phase ran exactly its share, the guard saw no cluster-global write left
   behind by a test, and the counters show no committed shared-catalog write
   across the parallel phase (a write undone within one test included).

Both test phases measure coverage with coverage.py's ``sys.monitoring`` core
(``COVERAGE_CORE=sysmon``, Python >= 3.12, line coverage): the same targets
and the same measured lines as the default C tracer, at a fraction of its
per-call overhead. coverage.py warns and falls back to its default core if
sysmon ever becomes unusable (branch coverage before 3.14, dynamic contexts).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ops.testing.ci_pytest_plugin import Counters, catalog_writes, read_counters
from ops.testing.isolation_common import ensure_private_directory
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPOSITORY: Final = Path(__file__).resolve().parents[2]
COVERAGE_TARGETS_FILE: Final = REPOSITORY / "ops/testing/coverage-targets.txt"
PLUGIN: Final = ("-p", "ops.testing.ci_pytest_plugin")
COVERAGE_FLOOR: Final = "--cov-fail-under=90"
PYTEST_ENVIRONMENT: Final = {"COVERAGE_CORE": "sysmon"}
MAX_WORKERS: Final = 32
PHASES: Final = ("parallel", "serial")
# PostgreSQL publishes a backend's pending counts when it closes, and while it
# is connected within 10 s (PGSTAT_IDLE_INTERVAL) of going idle.
COUNTER_LAG_SECONDS: Final = 10.0
# Bound on waiting for the phase's backends to exit before the final reading.
QUIESCE_SECONDS: Final = 120.0
MAX_SUSPECTS: Final = 40
# Client backends that connected since the driver's own admin session: the
# migrate command and every test process. A backend publishes its counts
# before it leaves pg_stat_activity.
PHASE_BACKENDS: Final = """
SELECT a.pid, a.backend_type, coalesce(a.datname, ''),
       coalesce(a.application_name, '')
FROM pg_catalog.pg_stat_activity AS a
WHERE a.pid <> pg_catalog.pg_backend_pid()
  AND a.backend_type IN ('client backend', 'parallel worker')
  AND a.backend_start >= %s
ORDER BY a.pid
"""
# Every pg_database column except identity, vacuum horizons and the ACL
# (compared separately), so a clone differing in owner, encoding, locale,
# connection limit or template flag fails closed on any PostgreSQL version.
TEMPLATE_ATTRIBUTES: Final = """
SELECT d.oid, pg_catalog.pg_get_userbyid(d.datdba),
       (pg_catalog.to_jsonb(d) - 'oid' - 'datname' - 'datfrozenxid'
        - 'datminmxid' - 'datacl')::text
FROM pg_catalog.pg_database AS d WHERE d.datname = %s
"""
DATABASE_ACL: Final = """
SELECT a.grantor::regrole::text, a.grantee, a.privilege_type, a.is_grantable
FROM pg_catalog.pg_database AS d,
     LATERAL pg_catalog.aclexplode(d.datacl) AS a
WHERE d.datname = %s
ORDER BY 1, 2, 3, 4
"""


def coverage_targets() -> tuple[str, ...]:
    """Return the authoritative ``--cov`` flags, one per non-comment line."""
    lines = COVERAGE_TARGETS_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(line for line in lines if line and not line.startswith("#"))


def main(argv: Sequence[str] | None = None) -> int:
    """Run collect, parallel and serial phases; return the job's exit code."""
    arguments = _parse(sys.argv[1:] if argv is None else argv)
    work_root = Path(arguments.work_root)
    if not work_root.is_absolute():
        _fail("--work-root must be absolute")
    work_root.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_directory(work_root)
    with runtime_directory(work_root, purpose="ci-pytest") as work:
        reference, manifest = _collect(work)
        expected_serial = {
            nodeid for nodeids in manifest.values() for nodeid in nodeids
        }
        with _worker_databases(arguments.workers) as window:
            parallel = _run(
                [
                    *PLUGIN,
                    "--ci-pytest-phase=parallel",
                    f"--ci-pytest-record={work}",
                    "--ci-pytest-guard",
                    f"--numprocesses={arguments.workers}",
                    "--dist=loadfile",
                    "--reuse-db",
                    *coverage_targets(),
                    "--cov-report=",
                    *arguments.pytest_args,
                    "tests",
                ]
            )
            catalog_failures = window.check(work)
        reports = ["--cov-report=term-missing"]
        if arguments.cov_xml is not None:
            reports.append(f"--cov-report=xml:{arguments.cov_xml}")
        serial = _run(
            [
                *PLUGIN,
                "--ci-pytest-phase=serial",
                f"--ci-pytest-record={work}",
                "--reuse-db",
                *coverage_targets(),
                "--cov-append",
                *reports,
                COVERAGE_FLOOR,
                *arguments.pytest_args,
                *_serial_paths(expected_serial),
            ]
        )
        failures = [*verify(work, reference, expected_serial), *catalog_failures]
        executed = {phase: len(_executed(work, phase)) for phase in PHASES}
    for failure in failures:
        _say(f"FAIL {failure}")
    _say(
        f"reference={len(reference)} "
        f"parallel={executed['parallel']} "
        f"serial={executed['serial']} "
        f"parallel_exit={parallel} serial_exit={serial} "
        f"checks={'passed' if not failures else 'FAILED'}"
    )
    if parallel != 0:
        return parallel
    if serial != 0:
        return serial
    return 1 if failures else 0


def verify(
    work: Path, reference: Sequence[str], expected_serial: set[str]
) -> list[str]:
    """Compare the executed populations with the reference collection."""
    failures: list[str] = []
    if len(set(reference)) != len(reference):
        failures.append("reference collection repeats a node ID")
    universe = set(reference)
    executed = {phase: _executed(work, phase) for phase in PHASES}
    expected = {"parallel": universe - expected_serial, "serial": expected_serial}
    for phase, nodeids in executed.items():
        missing = sorted(expected[phase] - nodeids)
        extra = sorted(nodeids - expected[phase])
        if missing:
            failures.append(f"{phase} phase did not run {len(missing)}: {missing[:20]}")
        if extra:
            failures.append(f"{phase} phase ran {len(extra)} unexpected: {extra[:20]}")
    if executed["parallel"] & executed["serial"]:
        failures.append("a node ID ran in both phases")
    if executed["parallel"] | executed["serial"] != universe:
        failures.append("executed population differs from the reference collection")
    failures.extend(_guard_violations(work))
    return failures


def _executed(work: Path, phase: str) -> set[str]:
    path = work / f"{phase}-reports.json"
    if not path.is_file():
        return set()
    reports: dict[str, list[dict[str, str]]] = json.loads(path.read_text("utf-8"))
    return {
        nodeid
        for nodeid, entries in reports.items()
        if any(entry["when"] == "teardown" for entry in entries)
    }


def _guard_violations(work: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(work.glob("guard-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            failures.append(
                "cluster-global PostgreSQL state changed during "
                f"{record['nodeid']} on {record['worker']} ({record['changed']}); "
                "a test that writes it must be classified server-state in "
                "ops/testing/ci-serial-tests.txt"
            )
    return failures


class CatalogWindow:
    """Cumulative shared-catalog counters across the parallel phase.

    The per-test fingerprint only sees state a test leaves behind. A write
    committed and undone within one test (``GRANT clinic_resolver TO
    clinic_app`` ... ``REVOKE``) is visible to every concurrent worker in
    between, yet leaves no trace in the catalogs. It does move PostgreSQL's
    cumulative counters: ``n_mod_since_analyze`` counts every committed row
    insert, update and delete, and no rolled-back one. The window reads them
    once all earlier backends have published their counts, and again once
    every backend of the phase has exited; any committed write in between,
    or any reason the readings cannot certify the window, fails the run.
    """

    def __init__(
        self, admin: psycopg.Connection[tuple[object, ...]], since: object
    ) -> None:
        """Bind the admin session and the time it connected; read nothing."""
        self.admin = admin
        self.since = since
        self.baseline: Counters | None = None

    def start(self) -> None:
        """Take the baseline reading once earlier backends have published."""
        # ANALYZE resets n_mod_since_analyze; analysing now keeps autovacuum
        # from doing it mid-phase (its threshold is 50+ committed writes).
        for name in sorted(read_counters(self.admin).catalogs):
            self.admin.execute(
                sql.SQL("ANALYZE pg_catalog.{}").format(sql.Identifier(name))
            )
        # Publish this session's own pending counts (the clones' DDL) now.
        self.admin.execute("SELECT pg_catalog.pg_stat_force_next_flush()")
        busy = self._quiesce()
        if busy is not None:
            _fail(busy)
        self.baseline = read_counters(self.admin)

    def check(self, work: Path) -> list[str]:
        """Return the failures for committed writes since the baseline."""
        if self.baseline is None:
            return ["shared-catalog counters have no baseline reading"]
        try:
            busy = self._quiesce()
            if busy is not None:
                return [busy]
            final = read_counters(self.admin)
        except psycopg.Error as error:
            return [f"shared-catalog counters are unreadable: {error}"]
        writes, problems = catalog_writes(self.baseline, final)
        failures = [
            f"shared-catalog counters cannot certify the parallel phase: {problem}"
            for problem in problems
        ]
        failures.extend(
            f"cluster-global PostgreSQL state was written during the parallel "
            f"phase: {name} {counts} (committed row writes, including ones "
            f"undone before any test ended); {_suspects(work, name)}; a test "
            "that writes it must be classified server-state in "
            "ops/testing/ci-serial-tests.txt"
            for name, counts in writes.items()
        )
        return failures

    def _quiesce(self) -> str | None:
        deadline = time.monotonic() + QUIESCE_SECONDS
        while True:
            rows = self.admin.execute(PHASE_BACKENDS, [self.since]).fetchall()
            if not rows:
                return None
            if time.monotonic() > deadline:
                return (
                    "shared-catalog counters are unreadable: backends of the run "
                    f"are still connected after {QUIESCE_SECONDS:.0f} s: {rows}"
                )
            time.sleep(0.05)


def _suspects(work: Path, catalog: str) -> str:
    """Name the tests that ran up to COUNTER_LAG_SECONDS before a write showed."""
    counted = [
        json.loads(line)
        for path in sorted(work.glob("counted-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    seen = [record for record in counted if catalog in record["writes"]]
    windows = [
        (str(nodeid), path.stem.removeprefix("windows-"), float(start), float(end))
        for path in sorted(work.glob("windows-*.json"))
        for nodeid, start, end in json.loads(path.read_text(encoding="utf-8"))
    ]
    if seen:
        first = min(seen, key=lambda record: float(record["ended"]))
        low, high = float(first["started"]) - COUNTER_LAG_SECONDS, first["ended"]
        where = f"first counted after {first['nodeid']} on {first['worker']}"
    else:
        # Published only when the writer's backend exited at the phase end.
        low = max((end for *_, end in windows), default=0.0) - COUNTER_LAG_SECONDS
        high = float("inf")
        where = "counted only after the last test"
    # Most recent first: a closing session publishes its counts at once, an
    # idle one within COUNTER_LAG_SECONDS.
    suspects = sorted(
        (
            (-end, f"{nodeid} on {worker}")
            for nodeid, worker, start, end in windows
            if end >= low and start <= high
        ),
    )
    names = [name for _, name in suspects]
    listed = names[:MAX_SUSPECTS] + (
        [f"... {len(names) - MAX_SUSPECTS} more"] if len(names) > MAX_SUSPECTS else []
    )
    return (
        f"{where}; the writer is normally among the tests running within "
        f"{COUNTER_LAG_SECONDS:.0f} s before that, most recent first: {listed}"
    )


def _collect(work: Path) -> tuple[list[str], dict[str, list[str]]]:
    code = _run(
        [
            *PLUGIN,
            "--ci-pytest-phase=collect",
            f"--ci-pytest-record={work}",
            "--collect-only",
            "--quiet",
            "--quiet",
            "tests",
        ],
        quiet=True,
    )
    if code != 0:
        _fail(f"reference collection failed with exit {code}")
    collected = json.loads((work / "collected.json").read_text("utf-8"))
    reference: list[str] = collected["items"]
    manifest: dict[str, list[str]] = collected["manifest"]
    stale = sorted(prefix for prefix, nodeids in manifest.items() if not nodeids)
    if stale:
        _fail(f"serial classifications match no collected test: {stale}")
    return reference, manifest


def _serial_paths(expected_serial: set[str]) -> list[str]:
    files = sorted({nodeid.split("::", 1)[0] for nodeid in expected_serial})
    if not files:
        _fail("the serial classification is empty")
    return files


@contextmanager
def _worker_databases(workers: int) -> Iterator[CatalogWindow]:
    """Migrate the test database once, clone it per worker, drop the clones."""
    owner_url = os.environ["MIGRATION_DATABASE_URL"]
    template = f"test_{conninfo_to_dict(owner_url)['dbname']}"
    clones = [f"{template}_gw{index}" for index in range(workers)]
    admin = psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname="postgres", autocommit=True
    )
    with admin:
        since = admin.execute("SELECT pg_catalog.now()").fetchone()
        if since is None:
            _fail("the admin session returned no time")
        try:
            yield from _cloned(admin, owner_url, template, clones, since[0])
        finally:
            _drop_concurrently(clones)
            _say(f"worker databases dropped: {', '.join(clones)}")


def _cloned(
    admin: psycopg.Connection[tuple[object, ...]],
    owner_url: str,
    template: str,
    clones: list[str],
    since: object,
) -> Iterator[CatalogWindow]:
    migrate = _run_command(
        [
            sys.executable,
            "manage.py",
            "migrate",
            "--run-syncdb",
            "--noinput",
            "--skip-checks",
            "--verbosity=0",
        ],
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "MIGRATION_DATABASE_URL": _rebind(owner_url, template),
        },
    )
    if migrate != 0:
        _fail(f"test database migration failed with exit {migrate}")
    attributes = admin.execute(TEMPLATE_ATTRIBUTES, [template]).fetchone()
    if attributes is None:
        _fail(f"test database {template} does not exist; run make db-bootstrap")
    settings = admin.execute(
        "SELECT 1 FROM pg_catalog.pg_db_role_setting WHERE setdatabase = %s",
        [attributes[0]],
    ).fetchall()
    if settings:
        _fail("database-scoped settings cannot be carried to worker clones")
    for clone in clones:
        _drop(admin, clone)
        _clone(admin, template, clone, str(attributes[1]))
        clone_attributes = admin.execute(TEMPLATE_ATTRIBUTES, [clone]).fetchone()
        if clone_attributes is None or clone_attributes[1:] != attributes[1:]:
            _fail(f"worker database {clone} attributes differ from template")
        acl = admin.execute(DATABASE_ACL, [template]).fetchall()
        if admin.execute(DATABASE_ACL, [clone]).fetchall() != acl:
            _fail(f"worker database {clone} ACL differs from template")
    _say(f"worker databases cloned from {template}")
    window = CatalogWindow(admin, since)
    window.start()
    yield window


def _clone(
    admin: psycopg.Connection[tuple[object, ...]],
    template: str,
    clone: str,
    owner: str,
) -> None:
    admin.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
            sql.Identifier(clone), sql.Identifier(template), sql.Identifier(owner)
        )
    )
    # Database-level privileges are not copied from a template; replay the
    # template's exact ACL (bootstrap.sql's REVOKE FROM PUBLIC + GRANTs).
    admin.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(clone))
    )
    grants = admin.execute(
        "SELECT a.grantee::regrole::text, a.privilege_type, a.is_grantable "
        "FROM pg_catalog.pg_database AS d, "
        "LATERAL pg_catalog.aclexplode(d.datacl) AS a "
        "WHERE d.datname = %s AND a.grantee <> 0 AND a.grantee <> d.datdba",
        [template],
    ).fetchall()
    for grantee, privilege, grantable in grants:
        admin.execute(
            sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                sql.SQL(str(privilege)),
                sql.Identifier(clone),
                sql.Identifier(str(grantee)),
                sql.SQL(" WITH GRANT OPTION" if grantable else ""),
            )
        )


def _drop_concurrently(clones: list[str]) -> None:
    """Drop every clone at once, each on its own session.

    Each DROP DATABASE discards its database's buffers, cancels its pending
    fsync requests and then waits for an immediate checkpoint. Issued one
    by one, the first drop's checkpoint still writes and fsyncs every other
    clone. Issued together, every cancellation reaches the checkpointer while
    it syncs (it absorbs requests every few fsyncs), so the dropped files are
    skipped.
    """
    with ThreadPoolExecutor(max_workers=len(clones)) as pool:
        futures = [pool.submit(_drop_on_own_session, clone) for clone in clones]
    for future in futures:
        future.result()


def _drop_on_own_session(clone: str) -> None:
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname="postgres", autocommit=True
    ) as session:
        _drop(session, clone)


def _drop(admin: psycopg.Connection[tuple[object, ...]], clone: str) -> None:
    admin.execute(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(clone))
    )


def _rebind(url: str, database: str) -> str:
    from urllib.parse import quote, urlsplit, urlunsplit  # noqa: PLC0415

    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(path=f"/{quote(database, safe='')}"))


def _run(pytest_args: list[str], *, quiet: bool = False) -> int:
    return _run_command(
        [sys.executable, "-m", "pytest", *pytest_args],
        PYTEST_ENVIRONMENT,
        quiet=quiet,
    )


def _run_command(
    argv: list[str], overrides: dict[str, str], *, quiet: bool = False
) -> int:
    environment = {**os.environ, **overrides}
    _say(" ".join(argv[1:]))
    completed = subprocess.run(  # noqa: S603 - fixed interpreter argv
        argv,
        cwd=REPOSITORY,
        env=environment,
        check=False,
        stdout=subprocess.DEVNULL if quiet else None,
    )
    return completed.returncode


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m ops.testing.ci_pytest")
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--cov-xml")
    parser.add_argument("pytest_args", nargs="*")
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.workers <= MAX_WORKERS:
        _fail(f"--workers must be between 1 and {MAX_WORKERS}")
    return arguments


def _say(message: str) -> None:
    sys.stdout.write(f"ci-pytest: {message}\n")
    sys.stdout.flush()


def _fail(message: str) -> Never:
    sys.stderr.write(f"ci-pytest: {message}\n")
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
