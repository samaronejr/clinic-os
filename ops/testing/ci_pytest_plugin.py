"""Pytest plugin behind ``python -m ops.testing.ci_pytest``.

The driver loads this module with ``-p ops.testing.ci_pytest_plugin``; a plain
``pytest tests`` run never imports it, so the default collection and every
test's semantics are unchanged. It provides three things:

* phase selection: ``parallel`` deselects the tests classified in
  ``ci-serial-tests.txt``, ``serial`` keeps only them, ``collect`` and
  ``observe`` select everything;
* records: the reference population (``collect``) and every executed node ID
  with its reports (``parallel``/``serial``/``observe``), written as JSON for
  the driver's population-equality check;
* the server-state guard: a fingerprint of every PostgreSQL shared catalog
  (plus ``pg_file_settings``) taken around each test. Any committed
  cluster-global write (role attributes or passwords, memberships,
  ``ALTER ROLE/DATABASE ... SET``, databases, tablespaces, ``ALTER SYSTEM``)
  changes it, because every committed row version carries a new ``xmin``.
  In the parallel phase a change means a test that must be serial ran next
  to other workers; the driver fails the run.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

import psycopg
import pytest
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Generator

PHASES: Final = ("collect", "parallel", "serial", "observe")
GUARDED_PHASES: Final = frozenset({"parallel", "observe"})
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
    if guard and phase not in GUARDED_PHASES:
        _fail("the server-state guard runs only in the parallel or observe phase")
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
        self.guard = (
            ServerStateGuard(record / f"guard-{worker}.jsonl") if guard else None
        )
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
        if self.phase == "observe":
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
            self.guard.after(item.nodeid, self.worker)

    def pytest_sessionfinish(self) -> None:
        """Persist this process's reports and close the guard connection."""
        if self.guard is not None:
            self.guard.close()
        if self.worker == "main" and self.phase != "collect":
            _write_json(self.record / f"{self.phase}-reports.json", self.reports)


class ServerStateGuard:
    """Detect committed writes to PostgreSQL's cluster-global state."""

    def __init__(self, violations: Path) -> None:
        """Bind the per-process violation log; connect lazily."""
        self.violations = violations
        self.connection: psycopg.Connection[tuple[object, ...]] | None = None
        self.query: sql.Composed | None = None
        self.last: dict[str, tuple[int, str]] | None = None

    def before(self) -> None:
        """Take the first fingerprint; later tests reuse the previous one."""
        if self.last is None:
            self.last = self._fingerprint()

    def after(self, nodeid: str, worker: str) -> None:
        """Compare with the pre-test fingerprint and log any change."""
        current = self._fingerprint()
        previous = self.last
        self.last = current
        if previous is None or current == previous:
            return
        changed = sorted(
            name
            for name in current.keys() | previous.keys()
            if current.get(name) != previous.get(name)
        )
        line = json.dumps({"nodeid": nodeid, "worker": worker, "changed": changed})
        with self.violations.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def close(self) -> None:
        """Release the guard's own backend."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None

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
