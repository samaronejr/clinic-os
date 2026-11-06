"""Run the unmodified statement regression with archived owners in memory."""

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

sys.dont_write_bytecode = True

import gates

PRE_FIX = "7193de9245b32a5ec2c67b2c13ae5a0f2205921a"
CORRECTED = "6a3c7666c9eb5f7736d21afa58e2a7be95425876"
NODE = (
    "tests/billing/test_psp_review.py::"
    "test_corrected_statement_at_the_same_provider_version_can_post"
)
JUNIT = gates.TAKEOVER / "red-statement-correction.junit.xml"
RUNTIME = gates.TAKEOVER / "red-statement-runtime.json"


def source(path):
    return subprocess.check_output(
        ["git", "show", PRE_FIX + ":" + path], cwd=gates.LANE, text=True
    )


def guard_body(sql):
    return sql.split("AS $f$", 1)[1].rsplit("$f$", 1)[0]


def snapshot():
    import django
    import pytest

    sys.path[:0] = [str(gates.LANE), str(gates.LANE / "tests")]
    django.setup()
    paths = (
        "apps/billing/migrations/0012_sandbox_psp.py",
        "apps/billing/migrations/_psp_guard_sql.py",
        "apps/billing/psp_posting.py",
    )
    archived = {path: source(path) for path in paths}
    current_guard = importlib.import_module(
        "apps.billing.migrations._psp_guard_sql"
    ).SQL
    guard = importlib.import_module("apps.billing.migrations._psp_guard_sql")
    guard_path = paths[1]
    exec(compile(archived[guard_path], f"<git:{PRE_FIX}:{guard_path}>", "exec"), guard.__dict__)
    assert "AND NOT (e.source='statement' AND e.decision='quarantined')" not in guard.SQL
    migration_sql = importlib.reload(
        importlib.import_module("apps.billing.migrations._psp_sql")
    )
    migration = importlib.import_module("apps.billing.migrations.0012_sandbox_psp")
    assert archived[paths[0]] == (gates.LANE / paths[0]).read_text()
    migration.Migration.operations[-1].sql = migration_sql.SQL
    posting = importlib.import_module("apps.billing.psp_posting")
    original_apply = posting.apply_psp_evidence
    posting_path = paths[2]
    exec(compile(archived[posting_path], f"<git:{PRE_FIX}:{posting_path}>", "exec"), posting.__dict__)
    replaced_aliases = []
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if namespace is None:
            continue
        for name, value in list(namespace.items()):
            if value is original_apply:
                namespace[name] = posting.apply_psp_evidence
                replaced_aliases.append(module.__name__ + "." + name)
    report = {
        "corrected_source_head": CORRECTED,
        "pre_fix_revision": PRE_FIX,
        "node": NODE,
        "substitution": "in-memory migration SQL and posting module; no source-file edits",
        "archived_source_sha256": {
            path: hashlib.sha256(text.encode()).hexdigest()
            for path, text in archived.items()
        },
        "pre_fix_guard_sql_sha256": hashlib.sha256(guard.SQL.encode()).hexdigest(),
        "pre_fix_migration_sql_sha256": hashlib.sha256(migration_sql.SQL.encode()).hexdigest(),
        "posting_code_filename": posting.apply_psp_evidence.__code__.co_filename,
        "rebound_aliases": replaced_aliases,
    }

    class SchemaIdentity:
        @pytest.fixture(scope="session", autouse=True)
        def capture_schema(self, django_db_setup, django_db_blocker):
            from django.db import connection

            with django_db_blocker.unblock(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), p.prosrc, "
                    "p.proowner::regrole::text FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='clinic_app' AND p.proname='billing_psp_guard'"
                )
                database, actual_body, owner = cursor.fetchone()
            assert actual_body == guard_body(guard.SQL)
            report.update(
                database=database,
                live_guard_owner=owner,
                live_pre_fix_guard_body_sha256=hashlib.sha256(actual_body.encode()).hexdigest(),
                live_guard_matches_pre_fix=True,
            )
            RUNTIME.write_text(json.dumps(report, indent=2) + "\n")
            yield

    try:
        result = int(pytest.main(sys.argv[2:], plugins=[SchemaIdentity()]))
        report["pytest_exit"] = result
    finally:
        if report.get("database"):
            import psycopg

            with psycopg.connect(
                os.environ["TEST_SUPERUSER_DATABASE_URL"],
                dbname=report["database"],
            ) as connection, connection.cursor() as cursor:
                cursor.execute(
                    current_guard.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
                )
                cursor.execute(
                    "SELECT p.prosrc FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='clinic_app' AND p.proname='billing_psp_guard'"
                )
                restored_body = cursor.fetchone()[0]
                assert restored_body == guard_body(current_guard)
            report["corrected_schema_restored"] = True
        report["tracked_source_diff"] = gates.capture(
            ["git", "diff", "--name-only", CORRECTED, "--"]
        )
        assert report["tracked_source_diff"] == ""
        assert gates.capture(["git", "rev-parse", "HEAD"]) == CORRECTED
        RUNTIME.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(result)


def run():
    unit = Path("/proc/self/cgroup").read_text().strip().rsplit("/", 1)[-1]
    limits = gates.capture([
        "systemctl", "--user", "show", unit,
        "-p", "MemoryMax", "-p", "MemorySwapMax", "-p", "MainPID",
    ])
    assert "MemoryMax=12884901888" in limits and "MemorySwapMax=0" in limits
    (gates.TAKEOVER / "red-statement-unit.txt").write_text(limits + "\n")
    assert gates.capture(["git", "rev-parse", "HEAD"]) == CORRECTED
    log = gates.run(
        "red-statement-correction",
        [
            gates.PYTHON, str(Path(__file__).resolve()), "snapshot",
            "-o", "junit_family=xunit1", "--reuse-db", "-q", "--tb=short",
            "--no-showlocals", NODE, "--junitxml=" + str(JUNIT),
            "--basetemp=" + str(gates.TAKEOVER / "review-temp"),
        ],
        {**gates.load_env(), "PYTHONDONTWRITEBYTECODE": "1"},
        expected=1,
    )
    cases = ElementTree.parse(JUNIT).getroot().findall(".//testcase")
    assert len(cases) == 1
    assert cases[0].get("name") == NODE.split("::")[1]
    failure = cases[0].find("failure")
    assert failure is not None and cases[0].find("error") is None
    assert "superseded" in "".join(failure.itertext())
    runtime = json.loads(RUNTIME.read_text())
    assert runtime["live_guard_matches_pre_fix"]
    assert runtime["corrected_schema_restored"]
    assert runtime["pytest_exit"] == 1 and runtime["tracked_source_diff"] == ""
    print("EXPECTED_STATEMENT_RED_CAPTURED exit=1 reason=superseded-not-matched", flush=True)
    print("RED_LOG=" + str(log.relative_to(gates.E)), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1] == "snapshot":
        snapshot()
    else:
        assert sys.argv[1] == "run"
        run()
