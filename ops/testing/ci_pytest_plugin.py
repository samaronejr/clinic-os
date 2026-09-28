"""Pytest plugin behind ``python -m ops.testing.ci_pytest``.

The driver loads this module with ``-p ops.testing.ci_pytest_plugin``; a plain
``pytest tests`` run never imports it, so the default collection and every
test's semantics are unchanged. It provides three things:

* phase selection: ``parallel`` deselects the tests classified in
  ``ci-serial-tests.txt``, ``serial`` keeps only them, ``collect`` selects
  everything;
* records: the reference population (``collect``) and every executed node ID
  with its reports (``parallel``/``serial``), written as JSON for the
  driver's population-equality check;
* the server-state guard (parallel phase), around each test:

  - a fingerprint of every PostgreSQL shared catalog (plus
    ``pg_file_settings``). Any committed cluster-global write that is still
    there when the test ends (role attributes or passwords, memberships,
    ``ALTER ROLE/DATABASE ... SET``, databases, tablespaces, ``ALTER
    SYSTEM``) changes it, because every committed row version carries a new
    ``xmin``; the driver fails the run and names the test.
  - a reading of the shared catalogs' cumulative ``pg_stat`` counters
    (:func:`read_counters`). A write that a test commits and undoes again
    (``GRANT`` then ``REVOKE``) leaves no fingerprint, but it still counts;
    the driver compares the counters across the whole phase and uses these
    per-test readings only to point at the writer.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

import psycopg
import pytest
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Generator

PHASES: Final = ("collect", "parallel", "serial")
MANIFEST: Final = Path(__file__).resolve().with_name("ci-serial-tests.txt")
CATEGORIES: Final = frozenset({"server-state", "worker-environment"})
# pg_shdepend is the one shared catalog outside the fingerprint: PostgreSQL
# writes it whenever a role-owned object (a table, a temp table, a grant) is
# created in ANY database, so it tracks per-database churn, not cluster
# configuration. Every server-wide setting it could reflect lives in another
# shared catalog, which stays fingerprinted.
UNFINGERPRINTED_SHARED: Final = frozenset({"pg_shdepend"})
SHARED_CATALOGS: Final = """
SELECT c.relname
FROM pg_catalog.pg_class AS c
WHERE c.relisshared
  AND c.relkind = 'r'
  AND c.relnamespace = 'pg_catalog'::pg_catalog.regnamespace
ORDER BY c.relname
"""
FILE_SETTINGS: Final = """
SELECT 'pg_file_settings', count(*), coalesce(md5(string_agg(
    concat_ws(E'\\x1f', sourcefile, sourceline, seqno, name, setting,
              applied, error),
    E'\\x1e' ORDER BY seqno)), '')
FROM pg_catalog.pg_file_settings
"""
# Cumulative tuple counters of the counted shared catalogs. PostgreSQL counts
# n_tup_ins/upd/del for every attempted write, committed or rolled back, but
# n_mod_since_analyze (its changed_tuples) only for committed ones; ANALYZE
# resets that one, which analyze_count/autoanalyze_count record.
SHARED_COUNTERS: Final = """
SELECT s.relname, s.n_tup_ins, s.n_tup_upd, s.n_tup_del,
       s.n_mod_since_analyze, s.analyze_count + s.autoanalyze_count
FROM pg_catalog.pg_stat_all_tables AS s
JOIN pg_catalog.pg_class AS c ON c.oid = s.relid
WHERE c.relisshared
  AND c.relkind = 'r'
  AND c.relnamespace = 'pg_catalog'::pg_catalog.regnamespace
ORDER BY s.relname
"""
# What else must hold for the counters to cover a window: counting is on,
# the server did not restart, the shared-object counters were not reset
# (pg_stat_database's datid 0 row) and the configuration was not reloaded.
COUNTER_SERVER_STATE: Final = """
SELECT pg_catalog.current_setting('track_counts'),
       pg_catalog.pg_postmaster_start_time()::text,
       pg_catalog.pg_conf_load_time()::text,
       coalesce((SELECT d.stats_reset::text FROM pg_catalog.pg_stat_database AS d
                 WHERE d.datid = 0), 'never')
"""
COUNTED_SHARED: Final = frozenset(
    {"pg_auth_members", "pg_authid", "pg_database", "pg_db_role_setting"}
)
_ENTRY: Final = re.compile(r"(?P<category>[a-z-]+) (?P<prefix>tests/\S+\.py(::\S+)?)")


@dataclass(frozen=True, slots=True)
class SerialEntry:
    """One classified node-ID prefix from ``ci-serial-tests.txt``."""

    category: str
    prefix: str

    def matches(self, nodeid: str) -> bool:
        """Match the exact node, its children and its parametrizations."""
        return nodeid == self.prefix or nodeid.startswith(
            (f"{self.prefix}::", f"{self.prefix}[")
        )


def load_manifest(path: Path = MANIFEST) -> tuple[SerialEntry, ...]:
    """Parse the closed manifest grammar; any other line fails closed."""
    entries: list[SerialEntry] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        match = _ENTRY.fullmatch(line)
        if match is None or match["category"] not in CATEGORIES:
            _fail(f"{path.name}:{number}: invalid serial classification")
        entry = SerialEntry(match["category"], match["prefix"])
        if any(entry.prefix == seen.prefix for seen in entries):
            _fail(f"{path.name}:{number}: duplicate serial classification")
        entries.append(entry)
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class CatalogCounters:
    """One shared catalog's cumulative ``pg_stat`` tuple counters."""

    inserted: int
    updated: int
    deleted: int
    committed: int
    analyzed: int


@dataclass(frozen=True, slots=True)
class Counters:
    """A reading of every counted shared catalog plus the server state."""

    server: tuple[str, ...]
    catalogs: dict[str, CatalogCounters]


def read_counters(connection: psycopg.Connection[tuple[object, ...]]) -> Counters:
    """Read the shared catalogs' counters (autocommit: a fresh stats snapshot)."""
    server = connection.execute(COUNTER_SERVER_STATE).fetchone()
    if server is None:
        _fail("the server state query returned no row")
    catalogs = {
        str(name): CatalogCounters(*(int(str(value)) for value in values))
        for name, *values in connection.execute(SHARED_COUNTERS).fetchall()
        if name not in UNFINGERPRINTED_SHARED
    }
    return Counters(tuple(str(value) for value in server), catalogs)


def catalog_writes(
    before: Counters, after: Counters
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Return the committed shared-catalog writes between two readings.

    The second value lists every reason the readings cannot certify the
    window (counting off, a restart, a reset, a reload, an ANALYZE that hid
    the committed counter, counters that went backwards); callers fail
    closed on it. Rolled-back writes move only the attempt counters and are
    not writes.
    """
    problems = _server_problems(before, after)
    writes: dict[str, dict[str, int]] = {}
    for name in sorted(set(before.catalogs) & set(after.catalogs)):
        delta, problem = _catalog_delta(
            name, before.catalogs[name], after.catalogs[name]
        )
        if problem is not None:
            problems.append(problem)
        elif delta["committed"]:
            writes[name] = delta
    return writes, problems


def _server_problems(before: Counters, after: Counters) -> list[str]:
    checks = (
        (
            before.server[0] == after.server[0] == "on",
            "track_counts is off, so writes are not counted",
        ),
        (before.server[1] == after.server[1], "the PostgreSQL server restarted"),
        (
            before.server[3] == after.server[3],
            "the shared-object statistics were reset",
        ),
        (before.server[2] == after.server[2], "the server configuration was reloaded"),
        (
            set(after.catalogs) >= COUNTED_SHARED,
            "the shared catalogs' counters are incomplete",
        ),
        (
            set(before.catalogs) == set(after.catalogs),
            "the set of shared catalogs changed",
        ),
    )
    return [problem for holds, problem in checks if not holds]


def _catalog_delta(
    name: str, old: CatalogCounters, new: CatalogCounters
) -> tuple[dict[str, int], str | None]:
    delta = {
        "inserted": new.inserted - old.inserted,
        "updated": new.updated - old.updated,
        "deleted": new.deleted - old.deleted,
        "committed": new.committed - old.committed,
    }
    attempted = delta["inserted"] + delta["updated"] + delta["deleted"]
    if min(delta["inserted"], delta["updated"], delta["deleted"]) < 0:
        return delta, f"{name}: the counters went backwards"
    if new.analyzed != old.analyzed:
        if new.analyzed < old.analyzed or attempted:
            return delta, (
                f"{name}: ANALYZE reset the committed-write counter while "
                f"{attempted} row writes were attempted"
            )
        return {**delta, "committed": 0}, None
    if not 0 <= delta["committed"] <= attempted:
        return delta, f"{name}: the committed-write counter is inconsistent"
    return delta, None


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the driver-only options."""
    group = parser.getgroup("ci-pytest", "hosted Python job phases")
    group.addoption("--ci-pytest-phase", choices=PHASES, default=None)
    group.addoption("--ci-pytest-record", default=None)
    group.addoption("--ci-pytest-guard", action="store_true", default=False)


def pytest_configure(config: pytest.Config) -> None:
    """Activate the phase plugin only when the driver asked for it."""
    phase = config.getoption("--ci-pytest-phase")
    record = config.getoption("--ci-pytest-record")
    guard = bool(config.getoption("--ci-pytest-guard"))
    if phase is None:
        if record is not None or guard:
            _fail("--ci-pytest-record/--ci-pytest-guard need --ci-pytest-phase")
        return
    if record is None or not Path(record).is_absolute() or not Path(record).is_dir():
        _fail("--ci-pytest-record must name an existing absolute directory")
    if guard and phase != "parallel":
        _fail("the server-state guard runs only in the parallel phase")
    workerinput = getattr(config, "workerinput", None)
    worker = "main" if workerinput is None else str(workerinput["workerid"])
    config.pluginmanager.register(
        CiPhase(phase, Path(record), worker, guard=guard), "ci-pytest-phase"
    )


class CiPhase:
    """Select, record and guard one phase inside one pytest process."""

    def __init__(self, phase: str, record: Path, worker: str, *, guard: bool) -> None:
        """Bind the phase, record directory and worker identity."""
        self.phase = phase
        self.record = record
        self.worker = worker
        self.guard = ServerStateGuard(record, worker) if guard else None
        self.reports: dict[str, list[dict[str, str]]] = {}

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        """Apply the serial classification after every other selection hook."""
        entries = load_manifest()
        serial = {
            item.nodeid
            for item in items
            if any(entry.matches(item.nodeid) for entry in entries)
        }
        if self.phase == "collect":
            matches = {
                entry.prefix: [i.nodeid for i in items if entry.matches(i.nodeid)]
                for entry in entries
            }
            _write_json(
                self.record / "collected.json",
                {"items": [item.nodeid for item in items], "manifest": matches},
            )
            return
        keep_serial = self.phase == "serial"
        selected = [item for item in items if (item.nodeid in serial) == keep_serial]
        deselected = [item for item in items if (item.nodeid in serial) != keep_serial]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = selected

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Record every report the reporting process sees (xdist controller)."""
        if self.worker != "main":
            return
        self.reports.setdefault(report.nodeid, []).append(
            {"when": report.when, "outcome": report.outcome}
        )

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(
        self,
        item: pytest.Item,
        nextitem: pytest.Item | None,  # noqa: ARG002 - pytest hook signature
    ) -> Generator[None, object, object]:
        """Fingerprint shared server state around each test when guarded."""
        if self.guard is None:
            return (yield)
        self.guard.before()
        try:
            return (yield)
        finally:
            self.guard.after(item.nodeid)

    def pytest_sessionfinish(self) -> None:
        """Persist this process's reports and close the guard connection."""
        if self.guard is not None:
            self.guard.close()
        if self.worker == "main" and self.phase != "collect":
            _write_json(self.record / f"{self.phase}-reports.json", self.reports)


class ServerStateGuard:
    """Detect committed writes to PostgreSQL's cluster-global state.

    Per worker it writes, into the record directory: ``guard-<worker>.jsonl``
    (fingerprint changes: violations), ``counted-<worker>.jsonl`` (tests
    after which the cumulative counters showed a committed shared-catalog
    write, or could not certify the window) and ``windows-<worker>.json``
    (every test's wall-clock window), which the driver uses to attribute a
    phase-level counter change.
    """

    def __init__(self, record: Path, worker: str) -> None:
        """Bind the record directory and worker identity; connect lazily."""
        self.record = record
        self.worker = worker
        self.connection: psycopg.Connection[tuple[object, ...]] | None = None
        self.query: sql.Composed | None = None
        self.last: dict[str, tuple[int, str]] | None = None
        self.last_counters: Counters | None = None
        self.started = 0.0
        self.windows: list[tuple[str, float, float]] = []

    def before(self) -> None:
        """Take the first readings; later tests reuse the previous ones."""
        self.started = time.time()
        if self.last is None:
            self.last = self._fingerprint()
            self.last_counters = read_counters(self._connect())

    def after(self, nodeid: str) -> None:
        """Compare with the pre-test readings and log any change."""
        current = self._fingerprint()
        counters = read_counters(self._connect())
        ended = time.time()
        self.windows.append((nodeid, self.started, ended))
        previous, previous_counters = self.last, self.last_counters
        self.last, self.last_counters = current, counters
        if previous_counters is not None:
            writes, problems = catalog_writes(previous_counters, counters)
            if writes or problems:
                self._append(
                    f"counted-{self.worker}.jsonl",
                    {
                        "nodeid": nodeid,
                        "worker": self.worker,
                        "started": self.started,
                        "ended": ended,
                        "writes": writes,
                        "problems": problems,
                    },
                )
        if previous is None or current == previous:
            return
        changed = sorted(
            name
            for name in current.keys() | previous.keys()
            if current.get(name) != previous.get(name)
        )
        self._append(
            f"guard-{self.worker}.jsonl",
            {"nodeid": nodeid, "worker": self.worker, "changed": changed},
        )

    def close(self) -> None:
        """Persist the test windows and release the guard's own backend."""
        _write_json(self.record / f"windows-{self.worker}.json", self.windows)
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _append(self, name: str, record: dict[str, object]) -> None:
        with (self.record / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _fingerprint(self) -> dict[str, tuple[int, str]]:
        connection = self._connect()
        if self.query is None:
            names = [
                str(row[0])
                for row in connection.execute(SHARED_CATALOGS).fetchall()
                if row[0] not in UNFINGERPRINTED_SHARED
            ]
            if not {"pg_authid", "pg_db_role_setting", "pg_database"} <= set(names):
                _fail("shared catalog derivation is incomplete")
            parts: list[sql.Composable] = [
                sql.SQL(
                    "SELECT {name}, count(*), coalesce(md5(string_agg("
                    "xmin::text, ',' ORDER BY xmin::text)), '') "
                    "FROM pg_catalog.{relation}"
                ).format(name=sql.Literal(name), relation=sql.Identifier(name))
                for name in names
            ]
            parts.append(sql.SQL(FILE_SETTINGS))
            self.query = sql.SQL(" UNION ALL ").join(parts)
        rows = connection.execute(self.query).fetchall()
        return {
            str(name): (int(str(count)), str(digest)) for name, count, digest in rows
        }

    def _connect(self) -> psycopg.Connection[tuple[object, ...]]:
        if self.connection is None:
            self.connection = psycopg.connect(
                os.environ["TEST_SUPERUSER_DATABASE_URL"],
                dbname="postgres",
                autocommit=True,
                application_name="ci-pytest-guard",
            )
        return self.connection


def _write_json(path: Path, value: object) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _fail(message: str) -> Never:
    raise pytest.UsageError(message)
