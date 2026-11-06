"""Task-owned, secret-redacted lane gates; invoked only inside capped user units."""

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LANE = Path("/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2")
E = LANE / ".omo/evidence/clinic-ops-premium-intelligence/task-57"
TAKEOVER = E / "takeover"
PRIVATE = TAKEOVER / ".private-runtime.json"
CONTAINER = "cpi-t57-st01a1b176-pg"
VOLUME = "cpi-t57-st01a1b176-data"
OWNER = "st_01a1b176"
PYTHON = str(LANE / ".venv/bin/python")
UV = "/home/samarone/.local/bin/uv"
BASE = {
    **os.environ,
    "PATH": "/home/samarone/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "CLINIC_DATA_MODE": "synthetic",
    "DJANGO_SETTINGS_MODULE": "config.settings.test",
    "UV_PROJECT_ENVIRONMENT": str(LANE / ".venv"),
    "UV_NO_SYNC": "1",
    "UV_FROZEN": "1",
    "UV_NO_ENV_FILE": "1",
    "CELERY_BROKER_URL": "memory://",
    "CLINIC_PDF_TOOLS": "required",
    "GIT_PAGER": "cat",
}


def load_env():
    return {**BASE, **json.loads(PRIVATE.read_text())}


def redact(text, env):
    text = re.sub(r"postgres(?:ql)?://[^\s'\"<>]+", "<redacted-database-url>", text)
    for key, value in env.items():
        if ("PASSWORD" in key or key.endswith("_SECRET")) and value:
            text = text.replace(value, "<redacted-secret>")
    return text


def run(name, argv, env, *, expected=0):
    """Record a command's actual exit, without echoing environment or secrets."""
    name = env.get("T57_GATE_PREFIX", "") + name
    log = TAKEOVER / f"{name}.log"
    print(f"GATE_START {name}", flush=True)
    with log.open("w") as output:
        process = subprocess.Popen(
            argv, cwd=LANE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        for line in process.stdout:
            safe = redact(line, env)
            output.write(safe)
            output.flush()
            if re.search(
                r"passed|failed|ERROR|FAILED|All checks passed|Success:|No changes|"
                r"Would reformat|error:|OK$|IrreversibleError|posture", safe,
            ):
                print(safe.rstrip(), flush=True)
        code = process.wait()
    with (TAKEOVER / "commands.jsonl").open("a") as receipt:
        receipt.write(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "name": name, "argv": argv, "exit": code, "expected_exit": expected,
            "log": str(log.relative_to(E)),
        }) + "\n")
    print(f"GATE_EXIT {name} {code}", flush=True)
    if code != expected:
        print("\n".join(log.read_text().splitlines()[-50:]), flush=True)
        raise SystemExit(code or 1)
    return log


def capture(argv, env=BASE):
    result = subprocess.run(
        argv, cwd=LANE, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        print(redact(result.stderr, env), flush=True)
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def up():
    if PRIVATE.exists():
        raise SystemExit("private runtime already exists")
    for resource, command in ((CONTAINER, "inspect"), (VOLUME, "volume")):
        argv = ["docker", command, "inspect", resource] if command == "volume" else [
            "docker", "inspect", resource,
        ]
        probe = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            raise SystemExit("refusing an existing task resource")
    secret = secrets.token_hex(24)
    env = {
        **BASE, "POSTGRES_PASSWORD": secret,
        "CLINIC_OWNER_PASSWORD": secrets.token_hex(24),
        "CLINIC_APP_PASSWORD": secrets.token_hex(24),
        "CLINIC_SUPER_PASSWORD": secrets.token_hex(24),
        "CLINIC_AGENT_PASSWORD": secrets.token_hex(24),
    }
    capture(["docker", "volume", "create", "--label", f"clinic.owner={OWNER}", VOLUME])
    container_id = capture([
        "docker", "run", "-d", "--name", CONTAINER,
        "--label", f"clinic.owner={OWNER}", "--label", "clinic.task=57",
        "--memory=3g", "--memory-swap=3g",
        "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=clinic",
        "-p", "127.0.0.1::5432",
        "--mount", f"type=volume,source={VOLUME},destination=/var/lib/postgresql/data",
        "postgres:16.14",
    ], env)
    port = int(capture(["docker", "port", CONTAINER, "5432/tcp"]).rsplit(":", 1)[1])
    if port == 5432:
        raise SystemExit("forbidden host port")
    env.update({
        "POSTGRES_CONTAINER": CONTAINER, "POSTGRES_PORT": "5432",
        "POSTGRES_HOST_IP": "127.0.0.1", "POSTGRES_HOST_PORT": str(port),
        "POSTGRES_DB": "clinic", "POSTGRES_USER": "postgres",
        "TEST_DATABASE_NAME": "test_clinic",
        "APP_DATABASE_URL": f"postgresql://clinic_app:{env['CLINIC_APP_PASSWORD']}@127.0.0.1:{port}/clinic",
        "MIGRATION_DATABASE_URL": f"postgresql://clinic_owner:{env['CLINIC_OWNER_PASSWORD']}@127.0.0.1:{port}/clinic",
        "TEST_SUPERUSER_DATABASE_URL": f"postgresql://clinic_super:{env['CLINIC_SUPER_PASSWORD']}@127.0.0.1:{port}/clinic",
    })
    PRIVATE.write_text(json.dumps({k: v for k, v in env.items() if BASE.get(k) != v}))
    PRIVATE.chmod(0o600)
    resource_receipt = TAKEOVER / (env.get("T57_GATE_PREFIX", "") + "private-resource.json")
    resource_payload = json.dumps({
        "container": CONTAINER, "container_id": container_id, "volume": VOLUME,
        "host": "127.0.0.1", "host_port": port, "owner": OWNER,
        "container_memory": "3G", "container_swap": "0",
    }, indent=2) + "\n"
    (TAKEOVER / "private-resource.json").write_text(resource_payload)
    if resource_receipt.name != "private-resource.json":
        resource_receipt.write_text(resource_payload)
    run("postgres-ready", [
        "docker", "exec", CONTAINER, "sh", "-c",
        "until pg_isready -U postgres -d clinic >/dev/null 2>&1; do sleep 1; done",
    ], env)
    run("bootstrap", ["make", "db-bootstrap"], env)
    print("PRIVATE_DATABASE_READY", flush=True)


def static():
    env = load_env()
    run("ruff-check", [str(LANE / ".venv/bin/ruff"), "check", "."], env)
    run("ruff-format", [str(LANE / ".venv/bin/ruff"), "format", "--check", "."], env)
    run("mypy", [str(LANE / ".venv/bin/mypy"), "."], env)


def database():
    env = load_env()
    owner = {**env, "APP_DATABASE_URL": env["MIGRATION_DATABASE_URL"]}
    run("fresh-migrate", [PYTHON, "manage.py", "migrate", "--noinput"], owner)
    run("django-check", [PYTHON, "manage.py", "check"], owner)
    run("migration-drift", [PYTHON, "manage.py", "makemigrations", "--check", "--dry-run"], owner)
    run("db-posture", ["make", "db-posture"], env)


def targeted():
    env = load_env()
    run("targeted", [
        PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
        "tests/billing", "tests/renewal/test_invoices.py",
        "tests/renewal/test_billing_views.py", "tests/renewal/test_payment_reconciliation.py",
        "tests/renewal/test_pix_adapter.py",
        "tests/renewal/test_live_activation.py::test_live_preflight_rejects_invalid_environment",
        "tests/renewal/test_browser_runner.py",
        "tests/renewal/test_action_operations.py",
        "tests/renewal/test_integration_boundary.py",
        "tests/infra/test_schema_policy.py",
        "tests/infra/test_resolver_catalog.py", "tests/infra/test_module_boundaries.py",
        "tests/infra/test_recovery_manifest.py", "tests/audit/test_phase1_audit_contract.py",
        "tests/isolation/test_backup_restore_guard.py",
        "--junitxml=" + str(TAKEOVER / "targeted.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "pytest-temp"),
    ], env)


def qa():
    env = {
        **load_env(), "PYTHONPATH": f"{LANE}:{LANE / 'tests'}",
        "T57_EVIDENCE": str(E),
    }
    run("manual-qa", [
        PYTHON, str(TAKEOVER / "qa_runner.py"), str(TAKEOVER / "manual_qa.py"),
        "--reuse-db", "-q", "-s", "--tb=short", "--no-showlocals",
        "--junitxml=" + str(TAKEOVER / "manual-qa.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "qa-temp"),
    ], env)


def broker():
    sys.path[:0] = [str(LANE), str(LANE / "tests")]
    from billing.psp_broker_support import event_process
    from ops.testing.realtime_stack import REDIS_IMAGE
    env = {**load_env(), "CLINIC_BROKER_GATE": "required"}
    name = "cpi-t57-st01a1b176-redis"
    probe = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert probe.returncode != 0, "refusing an existing broker resource"
    container_id = capture(["docker", "run", "-d", "--name", name,
        "--label", f"clinic.owner={OWNER}", "--label", "clinic.task=57",
        "--memory=256m", "--memory-swap=256m", "-p", "127.0.0.1::6379",
        REDIS_IMAGE, "redis-server", "--save", "", "--appendonly", "no"], env)
    try:
        port = int(capture(["docker", "port", name, "6379/tcp"]).rsplit(":", 1)[1])
        assert port != 6379
        env["CLINIC_PSP_GATE_BROKER_URL"] = f"redis://127.0.0.1:{port}/0"
        (TAKEOVER / "broker-resource.json").write_text(json.dumps({
            "container": name, "container_id": container_id, "owner": OWNER,
            "host_port": port, "memory_bytes": 268435456, "swap_bytes": 0,
        }, indent=2) + "\n")
        with event_process(("/usr/bin/docker", "logs", "--follow", name),
                           cwd=LANE, environment=env, ready_line="Ready to accept connections"):
            run("psp-broker", [
                PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
                "tests/billing/test_psp_broker.py",
                "--junitxml=" + str(TAKEOVER / "psp-broker.junit.xml"),
                "--basetemp=" + str(TAKEOVER / "replay-temp"),
            ], env)
    finally:
        label = capture(["docker", "inspect", "-f", '{{ index .Config.Labels "clinic.owner" }}', name])
        assert label == OWNER
        run("remove-private-broker", ["docker", "rm", "-f", "-v", name], env)

def reviewed():
    run("reviewed-modules", [
        PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
        "tests/billing/test_legacy_replay_scope.py",
        "tests/billing/test_finance_views.py",
        "--junitxml=" + str(TAKEOVER / "reviewed-modules.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "review-temp"),
    ], load_env())


def catalog():
    env = load_env()
    os.environ.update(env)
    sys.path.insert(0, str(LANE))
    sys.path.insert(0, str(LANE / "tests"))
    import django
    django.setup()
    from django.db import connection
    from infra.psp_catalog import TABLES
    from ops.testing import restore_contract

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
            "c.relowner::regrole::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relkind IN ('r','p') ORDER BY c.relname"
        )
        relations = cursor.fetchall()
        derived = {row[0] for row in relations} - set(restore_contract.EXCLUDED_RELATIONS)
        cursor.execute(
            "SELECT table_name,grantee,privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND table_name=ANY(%s) "
            "ORDER BY table_name,grantee,privilege_type", [sorted(TABLES)],
        )
        grants = cursor.fetchall()
        cursor.execute(
            "SELECT table_name,column_name,grantee,privilege_type "
            "FROM information_schema.role_column_grants WHERE table_schema='clinic_app' "
            "AND table_name=ANY(%s) AND privilege_type='UPDATE' "
            "ORDER BY table_name,column_name,grantee", [sorted(TABLES)],
        )
        columns = cursor.fetchall()
        cursor.execute(
            "SELECT p.proname,pg_get_function_identity_arguments(p.oid),"
            "pg_get_function_result(p.oid),p.prosecdef,p.provolatile,p.proconfig,"
            "p.proowner::regrole::text,p.proacl::text "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='clinic_app' AND p.proname LIKE 'billing_%' ORDER BY p.proname"
        )
        functions = cursor.fetchall()
    report = {
        "origin": "fresh PostgreSQL 16.14 live catalog",
        "relation_count": len(relations), "excluded_count": len(relations) - len(derived),
        "derived_domain_count": len(derived),
        "manifest_domain_count": len(restore_contract.DOMAIN_RELATIONS),
        "unclassified": sorted(derived - set(restore_contract.DOMAIN_RELATIONS)),
        "missing": sorted(set(restore_contract.DOMAIN_RELATIONS) - derived),
        "journal_posture": [row for row in relations if row[0] in TABLES],
        "table_grants": grants, "update_column_grants": columns, "functions": functions,
    }
    (TAKEOVER / "live-catalog.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {
        "journal_posture", "table_grants", "update_column_grants", "functions",
    }}), flush=True)
    if report["missing"] or report["unclassified"]:
        raise SystemExit("recovery manifest differs from live catalog")


def clear_failed_test_data():
    run("failed-run-flush", [
        PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short",
        "tests/billing/test_psp_posture.py::test_psp_tables_are_forced_rls_append_only_and_have_exact_policies",
        "--junitxml=" + str(TAKEOVER / "failed-run-flush.junit.xml"),
    ], load_env())


def red_review():
    log = run("red-evidence-authority", [
        PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
        "tests/billing/test_psp_review.py::test_financial_read_authority_cannot_fabricate_another_actors_evidence",
        "--junitxml=" + str(TAKEOVER / "red-evidence-authority.junit.xml"),
    ], load_env(), expected=1)
    assert "DID NOT RAISE" in log.read_text() and "2 failed" in log.read_text()


def affected():
    env = load_env()
    paths = [str(path.relative_to(LANE)) for path in sorted((LANE / "tests/billing").glob("test_psp*.py")) if path.name != "test_psp_broker.py"]
    paths += ["tests/renewal/test_invoices.py", "tests/renewal/test_live_activation.py",
              "tests/identity/test_sql_guard_inventory.py", "tests/infra/test_module_boundaries.py",
              "tests/infra/test_resolver_catalog.py", "tests/infra/test_schema_policy.py",
              "tests/infra/test_recovery_manifest.py", "tests/isolation/test_backup_restore_guard.py",
              "tests/audit/test_phase1_audit_contract.py"]
    run("affected", [PYTHON, "-m", "pytest", "-o", "junit_family=xunit1", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
         *paths, "--junitxml=" + str(TAKEOVER / "affected.junit.xml"),
         "--basetemp=" + str(TAKEOVER / "affected-temp")], env)


def serial():
    prefixes = []
    for line in (LANE / "ops/testing/ci-serial-tests.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        _, prefix = line.split()
        if re.match(r"tests/(identity|retention|infra|isolation)/", prefix) or re.match(
            r"tests/renewal/(test_action_operations|test_browser_runner|test_invoices|test_live_activation)\.py", prefix
        ):
            prefixes.append(prefix)
    assert prefixes
    prefixes.sort(key=lambda nodeid: "test_every_authorization_candidate_is_accounted_for" not in nodeid)
    (TAKEOVER / "serial-selection.json").write_text(json.dumps(prefixes, indent=2) + "\n")
    run("serial", [PYTHON, "-m", "pytest", "-o", "junit_family=xunit1", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
         *prefixes, "--junitxml=" + str(TAKEOVER / "serial.junit.xml"),
         "--basetemp=" + str(TAKEOVER / "serial-temp")], load_env())


def discovery():
    env = {**load_env(), "PYTHONPATH": f"{LANE}:{LANE / 'tests'}"}
    run("discovery", [PYTHON, str(TAKEOVER / "generate_psp_inventory.py")], env)


def review_green():
    run("green-review", [PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short", "--no-showlocals",
        "tests/billing/test_psp_review.py", "tests/billing/test_psp_adapter.py",
        "tests/billing/test_psp_outbox.py", "tests/billing/test_psp_statement.py",
        "tests/billing/test_psp_refunds.py", "tests/billing/test_psp_patient.py",
        "--junitxml=" + str(TAKEOVER / "green-review.junit.xml")], load_env())


def auxiliary():
    run("dependency-audit", [str(LANE / ".venv/bin/pip-audit"), "--local"], load_env())
    paths = ["docs/plans/clinic-os-phase1a-approved.md", "docs/plans/clinic-os-phase1a-approved.sha256"]
    import hashlib
    receipt = {}
    for relative in paths:
        original = subprocess.check_output(["git", "show", "ae1b3e4c:" + relative], cwd=LANE)
        current = (LANE / relative).read_bytes()
        assert current == original
        receipt[relative] = hashlib.sha256(current).hexdigest()
    (TAKEOVER / "frozen-artifacts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("FROZEN_ARTIFACTS_UNCHANGED", flush=True)


def qa_refund():
    env = {**load_env(), "PYTHONPATH": f"{LANE}:{LANE / 'tests'}",
           "T57_EVIDENCE": str(E)}
    run("manual-qa-refund", [PYTHON, str(TAKEOVER / "qa_runner.py"),
        "-q", "--reuse-db", "--tb=short", "--no-showlocals",
        str(TAKEOVER / "manual_qa.py") + "::test_manual_authenticated_refund_replay_has_one_linked_reversal",
        "--junitxml=" + str(TAKEOVER / "manual-qa-refund.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "qa-refund-temp")], env)


def presentation():
    run("native-presentation", [PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short",
        "tests/billing/test_psp_patient.py::test_native_psp_partial_labels_nonpayable_instructions_and_installments",
        "--junitxml=" + str(TAKEOVER / "native-presentation.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "presentation-temp")], load_env())


def signatures():
    run("psp-signature-pins", [PYTHON, "-m", "pytest", "--reuse-db", "-q", "--tb=short",
        "tests/infra/test_module_boundaries.py::test_psp_human_boundaries_derive_authority_from_current_context",
        "--junitxml=" + str(TAKEOVER / "psp-signature-pins.junit.xml")], load_env())


def serial_census_retry():
    run("serial-census-retry", [PYTHON, "-m", "pytest", "-o", "junit_family=xunit1",
        "--reuse-db", "-q", "--tb=short", "--no-showlocals",
        "tests/identity/test_permission_parity.py::test_every_authorization_candidate_is_accounted_for",
        "--junitxml=" + str(TAKEOVER / "serial-census-retry.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "serial-census-temp")], load_env())


def role_regression():
    run("reader-writer-regression", [PYTHON, "-m", "pytest", "-o", "junit_family=xunit1",
        "--reuse-db", "-q", "--tb=short", "--no-showlocals",
        "tests/billing/test_psp_review.py::test_financial_read_authority_cannot_fabricate_another_actors_evidence",
        "--junitxml=" + str(TAKEOVER / "reader-writer-regression.junit.xml"),
        "--basetemp=" + str(TAKEOVER / "role-review-temp")], load_env())


def role_static():
    env = load_env()
    path = "tests/billing/test_psp_review.py"
    run("role-ruff-check", [str(LANE / ".venv/bin/ruff"), "check", path], env)
    run("role-ruff-format", [str(LANE / ".venv/bin/ruff"), "format", "--check", path], env)
    run("role-mypy", [str(LANE / ".venv/bin/mypy"), path], env)


def cleanup():
    import shutil
    env = load_env()
    resource = json.loads((TAKEOVER / "private-resource.json").read_text())
    container_id = capture(["docker", "inspect", "-f", "{{.Id}}", CONTAINER])
    if container_id != resource["container_id"]:
        raise SystemExit("resource identity mismatch")
    label = capture(["docker", "inspect", "-f", '{{ index .Config.Labels "clinic.owner" }}', CONTAINER])
    if label != OWNER:
        raise SystemExit("resource owner mismatch")
    run("remove-private-postgres", ["docker", "rm", "-f", CONTAINER], env)
    volume_label = capture([
        "docker", "volume", "inspect", "-f", '{{ index .Labels "clinic.owner" }}', VOLUME,
    ])
    if volume_label != OWNER:
        raise SystemExit("volume owner mismatch")
    run("remove-private-volume", ["docker", "volume", "rm", VOLUME], env)
    PRIVATE.unlink()
    removed = []
    for relative in (
        "pytest-temp", "qa-temp", "qa-refund-temp", "replay-temp", "review-temp",
        "presentation-temp", "affected-temp", "serial-temp", "serial-census-temp", "review-green-temp",
        "role-review-temp",
    ):
        path = TAKEOVER / relative
        if path.exists():
            shutil.rmtree(path)
            removed.append(relative)
    remaining = capture([
        "docker", "ps", "-a", "--filter", f"label=clinic.owner={OWNER}",
        "--format", "{{.ID}}",
    ])
    assert not remaining
    password_bytes = [
        value.encode() for key, value in env.items()
        if key.endswith("_PASSWORD") and isinstance(value, str) and value
    ]
    checked_files = 0
    password_leaks = []
    for path in E.rglob("*"):
        if not path.is_file():
            continue
        checked_files += 1
        content = path.read_bytes()
        if any(secret in content for secret in password_bytes):
            password_leaks.append(str(path.relative_to(E)))
    assert not password_leaks, ("private runtime credential in evidence", password_leaks)
    (TAKEOVER / "cleanup-receipt.json").write_text(json.dumps({
        "owner": OWNER, "postgres_container_id": container_id,
        "postgres_removed": True, "volume_removed": True,
        "runtime_secrets_removed": not PRIVATE.exists(),
        "temporary_evidence_directories_removed": removed,
        "remaining_owned_containers": [],
        "private_password_scan_checked_files": checked_files,
        "private_password_scan_leaks": password_leaks,
    }, indent=2) + "\n")
    print("PRIVATE_RESOURCE_CLEANUP_PASS", flush=True)


def commit():
    run("commit", [
        "git", "commit", "-F", str(TAKEOVER / "COMMIT_MESSAGE.txt"),
    ], load_env())
    print("COMMITTED_HEAD=" + capture(["git", "rev-parse", "HEAD"]), flush=True)


def review_commit():
    run("review-commit", [
        "git", "commit", "-F", str(TAKEOVER / "REVIEW_COMMIT_MESSAGE.txt"),
    ], load_env())
    print("COMMITTED_HEAD=" + capture(["git", "rev-parse", "HEAD"]), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    unit = Path("/proc/self/cgroup").read_text().strip().rsplit("/", 1)[-1]
    limits = capture(["systemctl", "--user", "show", unit,
                      "-p", "MemoryMax", "-p", "MemorySwapMax", "-p", "MainPID"])
    assert "MemoryMax=12884901888" in limits and "MemorySwapMax=0" in limits
    (TAKEOVER / f"{BASE.get('T57_GATE_PREFIX', '')}{sys.argv[1]}-unit.txt").write_text(limits + "\n")
    globals()[sys.argv[1]]()
