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
   so the superuser clones; the clones are dropped afterwards.
3. ``parallel``: ``pytest -n N --dist loadfile`` over everything not
   classified serial, with the server-state guard on.
4. ``serial``: one process runs the classified tests alone, appends to the
   same coverage data and applies the unchanged reports and
   ``--cov-fail-under=90`` to the combined total.
5. Checks: parallel executed + serial executed == reference, disjointly, each
   phase ran exactly its share, and the guard saw no cluster-global write.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ops.testing.isolation_common import ensure_private_directory
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPOSITORY: Final = Path(__file__).resolve().parents[2]
COVERAGE_TARGETS_FILE: Final = REPOSITORY / "ops/testing/coverage-targets.txt"
PLUGIN: Final = ("-p", "ops.testing.ci_pytest_plugin")
COVERAGE_FLOOR: Final = "--cov-fail-under=90"
MAX_WORKERS: Final = 32
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
        with _worker_databases(arguments.workers) as template:
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
            _say(f"worker databases cloned from {template}")
        failures = verify(work, reference, expected_serial)
        summary = _summary(work, reference, expected_serial, failures)
        if arguments.report is not None:
            Path(arguments.report).write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    for failure in failures:
        _say(f"FAIL {failure}")
    _say(
        f"reference={len(reference)} "
        f"parallel={summary['parallel_executed']} "
        f"serial={summary['serial_executed']} "
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
    executed = {phase: _executed(work, phase) for phase in ("parallel", "serial")}
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


def _summary(
    work: Path, reference: Sequence[str], expected_serial: set[str], failures: list[str]
) -> dict[str, object]:
    outcomes: dict[str, dict[str, str]] = {}
    for phase in ("parallel", "serial"):
        path = work / f"{phase}-reports.json"
        if path.is_file():
            reports: dict[str, list[dict[str, str]]] = json.loads(
                path.read_text("utf-8")
            )
            outcomes[phase] = {
                nodeid: _outcome(entries) for nodeid, entries in reports.items()
            }
    return {
        "failures": failures,
        "outcomes": outcomes,
        "parallel_executed": len(_executed(work, "parallel")),
        "reference": list(reference),
        "serial_executed": len(_executed(work, "serial")),
        "serial_expected": sorted(expected_serial),
    }


def _outcome(entries: list[dict[str, str]]) -> str:
    """Fold setup/call/teardown reports into one pytest-style outcome."""
    for entry in entries:
        if entry["outcome"] == "failed":
            return "error" if entry["when"] != "call" else "failed"
    if any(entry["outcome"] == "skipped" for entry in entries):
        return "skipped"
    return "passed"


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
def _worker_databases(workers: int) -> Iterator[str]:
    """Migrate the owner-owned test database once and clone it per worker."""
    owner_url = os.environ["MIGRATION_DATABASE_URL"]
    template = f"test_{conninfo_to_dict(owner_url)['dbname']}"
    clones = [f"{template}_gw{index}" for index in range(workers)]
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
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname="postgres", autocommit=True
    ) as admin:
        attributes = admin.execute(TEMPLATE_ATTRIBUTES, [template]).fetchone()
        if attributes is None:
            _fail(f"test database {template} does not exist; run make db-bootstrap")
        settings = admin.execute(
            "SELECT 1 FROM pg_catalog.pg_db_role_setting WHERE setdatabase = %s",
            [attributes[0]],
        ).fetchall()
        if settings:
            _fail("database-scoped settings cannot be carried to worker clones")
        try:
            for clone in clones:
                _drop(admin, clone)
                _clone(admin, template, clone, str(attributes[1]))
                clone_attributes = admin.execute(
                    TEMPLATE_ATTRIBUTES, [clone]
                ).fetchone()
                if clone_attributes is None or clone_attributes[1:] != attributes[1:]:
                    _fail(f"worker database {clone} attributes differ from template")
                acl = admin.execute(DATABASE_ACL, [template]).fetchall()
                if admin.execute(DATABASE_ACL, [clone]).fetchall() != acl:
                    _fail(f"worker database {clone} ACL differs from template")
            yield template
        finally:
            for clone in clones:
                _drop(admin, clone)


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


def _drop(admin: psycopg.Connection[tuple[object, ...]], clone: str) -> None:
    admin.execute(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(clone))
    )


def _rebind(url: str, database: str) -> str:
    from urllib.parse import quote, urlsplit, urlunsplit  # noqa: PLC0415

    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(path=f"/{quote(database, safe='')}"))


def _run(pytest_args: list[str], *, quiet: bool = False) -> int:
    return _run_command([sys.executable, "-m", "pytest", *pytest_args], {}, quiet=quiet)


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
    parser.add_argument("--report")
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
