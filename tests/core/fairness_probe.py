"""Reproducible worker-level QA driver for per-tenant queue fairness.

Runs the todo-9 QA scenarios against a real Redis broker and real Celery
worker processes so the evidence proves enqueue/defer/admit through the
actual task transport, not just ``fair_acquire`` calls.

Usage (from the lane worktree, with the lane database URLs exported):

    uv run --frozen --no-sync --no-env-file python \
        tests/core/fairness_probe.py fair
    uv run --frozen --no-sync --no-env-file python \
        tests/core/fairness_probe.py redis-down
    uv run --frozen --no-sync --no-env-file python \
        tests/core/fairness_probe.py quota-forge

``fair`` seeds two synthetic organizations, publishes ai-batch quotas of
60 and 120 tasks/minute, enqueues 200 probe tasks per organization through
the real broker, and runs two real ``celery worker -Q ai-batch`` processes
that execute ``apps.ai.tasks.synthetic_probe`` (routed by the
``apps.ai.tasks.*`` route). Each probe calls ``acquire_or_defer``; deferred
probes re-enqueue with the 30-second countdown and are redelivered until
admitted. Outcomes are counted in Redis and written to the evidence JSON
with full provenance (command, exit code, commit SHA, container names).

Synthetic data only; never run against a live deployment.
"""

from __future__ import annotations

import json
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid as uuid_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

WORKTREE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKTREE))
sys.path.insert(0, str(WORKTREE / "tests"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
os.environ.setdefault("CLINIC_SECRET_BACKEND", "synthetic-file")

import django  # noqa: E402

django.setup()

import celery.signals  # noqa: E402
import redis  # noqa: E402
from apps.core import fairness  # noqa: E402
from apps.identity.clinic_configuration import (  # noqa: E402
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.models import (  # noqa: E402
    Clinic,
    Organization,
    User,
    UserClinicRole,
)
from apps.tenancy.db import tenant_context  # noqa: E402
from config.celery import app as celery_app  # noqa: E402
from django.contrib.auth.hashers import make_password  # noqa: E402
from django.core.exceptions import ValidationError  # noqa: E402
from django.db import connection, transaction  # noqa: E402

from patient_service_support import runtime_role  # noqa: E402
from tenant_key_support import issue_tenant_key_for  # noqa: E402

if TYPE_CHECKING:
    from uuid import UUID

EVIDENCE_DIR = Path(
    "/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os/.omo/evidence/"
    "clinic-ops-premium-intelligence/task-9"
)
PROBE_TASK = "apps.ai.tasks.synthetic_probe"
PROBE_QUEUE = "ai-batch"
TASKS_PER_ORG = 200
WORKER_READY_TIMEOUT = 90.0
DRAIN_TIMEOUT = 600.0


@celery.signals.worker_ready.connect  # type: ignore[untyped-decorator]
def _signal_worker_ready(**_kwargs: object) -> None:
    """Write one byte to the driver's FIFO when this worker is ready."""
    fifo = os.environ.get("QA_WORKER_READY_FIFO")
    if not fifo:
        return
    fd = os.open(fifo, os.O_WRONLY)
    try:
        os.write(fd, b"1")
    finally:
        os.close(fd)


@celery_app.task(name=PROBE_TASK, bind=True)  # type: ignore[untyped-decorator]
def synthetic_probe(
    self: object, *, probe_id: str, organization_id: str, run_id: str
) -> str:
    """Metered no-op probe: acquire quota or defer with countdown.

    Named under ``apps.ai.tasks.*`` so the route table sends it to the
    ``ai-batch`` queue exactly like a future AI batch task. The outcome is
    recorded in Redis under the run id; a deferred probe re-enqueues via
    ``acquire_or_defer`` and is redelivered after the countdown.
    """
    org = uuid_module.UUID(organization_id)
    client = redis.Redis.from_url(str(celery_app.conf.broker_url))
    admitted = fairness.acquire_or_defer(
        self,  # type: ignore[arg-type]
        organization_id=org,
        queue=PROBE_QUEUE,
    )
    outcome = "admitted" if admitted else "deferred"
    first_key = f"cpi-t09-probe:{run_id}:first"
    if client.hsetnx(first_key, probe_id, outcome):
        client.rpush(f"cpi-t09-probe:{run_id}:events", f"first:{outcome}")
    client.hincrby(f"cpi-t09-probe:{run_id}:executions", outcome, 1)
    client.rpush(f"cpi-t09-probe:{run_id}:events", f"exec:{outcome}")
    return outcome


def _seed_org(label: str) -> tuple[UUID, UUID, UUID]:
    """Create one synthetic organization, clinic and org-wide admin."""
    organization_id = uuid_module.uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        Organization.objects.create(
            id=organization_id,
            name=f"Sintético QA {label}",
            cnpj=str(uuid_module.uuid4().int % 10**14).zfill(14),
        )
        clinic = Clinic.objects.create(
            organization_id=organization_id,
            name=f"Sintético Clinic {label}",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        user = User.objects.create(
            username=f"qa-t09-{label}-{uuid_module.uuid4().hex[:8]}",
            password=make_password("synthetic"),
        )
        UserClinicRole.objects.create(
            user_id=user.pk,
            organization_id=organization_id,
            clinic_id=clinic.pk,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    issue_tenant_key_for(organization_id)
    return organization_id, clinic.pk, user.pk


def _publish_quotas(
    organization_id: UUID, clinic_id: UUID, actor_id: UUID, quotas: dict[str, int]
) -> None:
    with runtime_role(), tenant_context(actor_id, organization_id):
        publish_configuration(
            clinic_id=clinic_id,
            expected_version=0,
            content=ConfigurationContent(
                display_name="Clínica Sintética", queue_quotas=quotas
            ),
        )


def _await_worker_readiness(
    fifo_fd: int,
    process: subprocess.Popen[bytes],
    log_path: Path,
) -> None:
    """Block until this worker writes to the readiness FIFO.

    The readiness byte is written by the ``worker_ready`` signal handler
    in this module, so arrival of the byte is the worker's own event, not
    a timed poll. ``select`` bounds the wait.
    """
    ready, _, _ = select.select([fifo_fd], [], [], WORKER_READY_TIMEOUT)
    if not ready or not os.read(fifo_fd, 1):
        process.terminate()
        if process.poll() is not None:
            message = f"celery worker exited during startup: {log_path}"
        else:
            message = f"celery worker did not signal readiness: {log_path}"
        raise RuntimeError(message)


def _start_worker(log_path: Path, ready_fifo: Path) -> subprocess.Popen[bytes]:
    """Spawn one real Celery worker bound to the ai-batch queue."""
    env = {
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "MIGRATION_DATABASE_URL": os.environ["APP_DATABASE_URL"],
        "CELERY_BROKER_URL": os.environ["CELERY_BROKER_URL"],
        "CLINIC_SECRET_BACKEND": "synthetic-file",
        "CLINIC_SECRET_DIR": os.environ["CLINIC_SECRET_DIR"],
        "CLINIC_DATA_MODE": "synthetic",
        "SECRET_KEY": "synthetic-worker-gate",
        "PYTHONPATH": f"{WORKTREE}:{WORKTREE / 'tests'}",
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - worker cwd fallback
        "QA_WORKER_READY_FIFO": str(ready_fifo),
    }
    log = log_path.open("ab")
    return subprocess.Popen(  # noqa: S603 - fixed module argv
        (
            sys.executable,
            "-m",
            "celery",
            "-A",
            "config.celery",
            "worker",
            "-Q",
            PROBE_QUEUE,
            "--concurrency=2",
            "--include",
            "core.fairness_probe",
            "-l",
            "INFO",
            "--hostname",
            f"qa-t09-{secrets.token_hex(4)}@%h",
        ),
        cwd=WORKTREE,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _provenance(command: str) -> dict[str, object]:
    git = shutil.which("git") or "git"
    sha = subprocess.run(  # noqa: S603 - fixed argv
        (git, "-C", str(WORKTREE), "rev-parse", "HEAD"),
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    return {
        "command": command,
        "worktree": str(WORKTREE),
        "commit_sha": sha,
        "postgres_container": os.environ.get("POSTGRES_CONTAINER", ""),
        "redis_container": os.environ.get("QA_REDIS_CONTAINER", ""),
        "broker_url": os.environ.get("CELERY_BROKER_URL", ""),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def scenario_fair() -> int:  # noqa: C901, PLR0912, PLR0915 - one linear QA scene
    """Two orgs enqueue 200 real tasks each through real ai-batch workers."""
    run_id = secrets.token_hex(8)
    # socket_timeout must exceed the BLPOP wait: redis-py 8 defaults it to
    # 5s, which would raise TimeoutError instead of returning None.
    client = redis.Redis.from_url(os.environ["CELERY_BROKER_URL"], socket_timeout=30)
    org_a, clinic_a, actor_a = _seed_org("fair-a")
    org_b, clinic_b, actor_b = _seed_org("fair-b")
    _publish_quotas(org_a, clinic_a, actor_a, {"ai-batch": 60})
    _publish_quotas(org_b, clinic_b, actor_b, {"ai-batch": 120})
    log_dir = Path(os.environ.get("CLINIC_SECRET_DIR", "/tmp"))  # noqa: S108
    ready_fifo = (
        Path(
            os.environ.get("CLINIC_SECRET_DIR", "/tmp")  # noqa: S108
        )
        / f"qa-t09-ready-{run_id}.fifo"
    )
    os.mkfifo(ready_fifo)
    # O_RDONLY|O_NONBLOCK registers the read end so worker writers can open
    # the FIFO; select() below supplies the bounded wait for each event.
    fifo_fd = os.open(ready_fifo, os.O_RDONLY | os.O_NONBLOCK)
    # A driver-held writer keeps the FIFO from reporting EOF (POLLHUP)
    # between worker readiness writes.
    fifo_sink = os.open(ready_fifo, os.O_WRONLY | os.O_NONBLOCK)
    workers: list[subprocess.Popen[bytes]] = []
    try:
        for i in range(2):
            worker_log = log_dir / f"qa-t09-worker-{i}.log"
            process = _start_worker(worker_log, ready_fifo)
            workers.append(process)
            _await_worker_readiness(fifo_fd, process, worker_log)
        org_key = f"cpi-t09-probe:{run_id}:org"
        for org in (org_a, org_b):
            for _ in range(TASKS_PER_ORG):
                probe_id = uuid_module.uuid4().hex
                client.hset(org_key, probe_id, str(org))
                synthetic_probe.apply_async(
                    kwargs={
                        "probe_id": probe_id,
                        "organization_id": str(org),
                        "run_id": run_id,
                    }
                )
        # Block on the probe event stream until every probe has a
        # first-execution outcome; workers push one event per execution.
        first_key = f"cpi-t09-probe:{run_id}:first"
        events_key = f"cpi-t09-probe:{run_id}:events"
        events_deadline = time.monotonic() + DRAIN_TIMEOUT
        first_seen = 0
        admitted_total = 0
        while first_seen < 2 * TASKS_PER_ORG:
            remaining = max(1, int(events_deadline - time.monotonic()))
            item = client.blpop(events_key, timeout=min(5, remaining))
            if item is None:
                if time.monotonic() >= events_deadline:
                    break
                continue
            _, event = item
            if isinstance(event, bytes) and event.startswith(b"first:"):
                first_seen += 1
            if event == b"exec:admitted":
                admitted_total += 1
        seen = first_seen
        first = client.hgetall(first_key)
        orgs = client.hgetall(org_key)
        per_org: dict[str, dict[str, object]] = {}
        for label, org in (("org_a", org_a), ("org_b", org_b)):
            outcomes = [
                first[probe]
                for probe, owner in orgs.items()
                if owner == str(org).encode() and probe in first
            ]
            per_org[label] = {
                "organization_id": str(org),
                "enqueued": TASKS_PER_ORG,
                "admitted_first": sum(
                    1 for outcome in outcomes if outcome == b"admitted"
                ),
                "deferred_first": sum(
                    1 for outcome in outcomes if outcome == b"deferred"
                ),
            }
        admitted_first = sum(1 for value in first.values() if value == b"admitted")
        deferred_first = sum(1 for value in first.values() if value == b"deferred")
        # Keep consuming the event stream until every deferred probe has
        # been redelivered and admitted; each execution pushes an event.
        while admitted_total < 2 * TASKS_PER_ORG:
            remaining = max(1, int(events_deadline - time.monotonic()))
            item = client.blpop(events_key, timeout=min(5, remaining))
            if item is None:
                if time.monotonic() >= events_deadline:
                    break
                continue
            _, event = item
            if event == b"exec:admitted":
                admitted_total += 1
        executions = client.hgetall(f"cpi-t09-probe:{run_id}:executions")
        result = {
            "scenario": (
                "two orgs enqueue 200 ai-batch probe tasks each through "
                "two real celery workers; deferred tasks re-enqueue with "
                "countdown and are redelivered until admitted"
            ),
            "provenance": _provenance(
                "uv run --frozen --no-sync --no-env-file python "
                "tests/core/fairness_probe.py fair"
            ),
            "run_id": run_id,
            "tasks_per_org": TASKS_PER_ORG,
            "quotas_per_minute": {"org_a": 60, "org_b": 120},
            "first_execution": {
                "seen": seen,
                "admitted": admitted_first,
                "deferred": deferred_first,
                "per_org": per_org,
            },
            "total_executions": {
                key.decode() if isinstance(key, bytes) else key: int(value)
                for key, value in executions.items()
            },
        }
        ok = (
            seen == 2 * TASKS_PER_ORG
            and admitted_first == 180
            and deferred_first == 220
            and per_org["org_a"]["admitted_first"] == 60
            and per_org["org_b"]["admitted_first"] == 120
            and int(executions.get(b"admitted", 0)) == 2 * TASKS_PER_ORG
        )
        result["verdict"] = "pass" if ok else "fail"
        result["exit_code"] = 0 if ok else 1
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_DIR / "fair.json").write_text(json.dumps(result, indent=2))
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return 0 if ok else 1
    finally:
        for worker in workers:
            worker.send_signal(signal.SIGTERM)
        for worker in workers:
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
        os.close(fifo_fd)
        os.close(fifo_sink)
        ready_fifo.unlink(missing_ok=True)
        client.delete(
            f"cpi-t09-probe:{run_id}:first",
            f"cpi-t09-probe:{run_id}:executions",
            f"cpi-t09-probe:{run_id}:org",
            f"cpi-t09-probe:{run_id}:events",
        )


def scenario_redis_down() -> int:
    """Point metering at a dead port: every queue defers except clinical."""
    org, clinic, actor = _seed_org("down")
    _publish_quotas(org, clinic, actor, {"ai-batch": 600})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    lines = []
    with runtime_role():
        lines.append(
            "redis up, ai-batch acquire: "
            f"{fairness.fair_acquire(organization_id=org, queue='ai-batch')}"
        )
    original = fairness._broker_url
    fairness._broker_url = lambda: f"redis://127.0.0.1:{dead_port}/0"
    try:
        with runtime_role():
            for queue in fairness.QUEUE_NAMES:
                outcome = fairness.fair_acquire(organization_id=org, queue=queue)
                lines.append(
                    f"redis down, {queue}: {'allowed' if outcome else 'deferred'}"
                )
    finally:
        fairness._broker_url = original
    expected_defer = set(fairness.QUEUE_NAMES) - fairness.FAIL_OPEN_QUEUES
    ok = all(
        ("deferred" in line) == (line.split(", ")[1].split(":")[0] in expected_defer)
        for line in lines[1:]
    )
    receipt = {
        "provenance": _provenance(
            "uv run --frozen --no-sync --no-env-file python "
            "tests/core/fairness_probe.py redis-down"
        ),
        "lines": lines,
        "exit_code": 0 if ok else 1,
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "redis-down.txt").write_text(
        json.dumps(receipt, indent=2) + "\n" + "\n".join(lines) + "\n"
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def scenario_quota_forge() -> int:
    """Forge quota maps through the service boundary; all must be rejected."""
    org, clinic, actor = _seed_org("forge")
    lines = []
    forged: list[dict[str, Any]] = [
        {"ai-batch": 999999},
        {"ai-batch": 0},
        {"ai-batch": -1},
        {"ai-batch": 1.5},
        {"ai-batch": "600"},
        {"ai-batch": True},
        {"ai-batch": [60]},
        {"ai-batch": None},
        {"ai-batch": {"nested": 1}},
        {"unknown-queue": 60},
        {"ai-batch": 60, "extra": 1},
    ]
    with runtime_role(), tenant_context(actor, org):
        for attempt in forged:
            try:
                publish_configuration(
                    clinic_id=clinic,
                    expected_version=0,
                    content=ConfigurationContent(
                        display_name="Clínica Sintética", queue_quotas=attempt
                    ),
                )
            except ValidationError as exc:
                # Only the closed-vocabulary validator counts as a
                # rejection; any other failure class fails loudly below.
                lines.append(f"forged {attempt!r} -> rejected ({type(exc).__name__})")
            else:
                lines.append(f"forged {attempt!r} -> ACCEPTED (BUG)")
        publish_configuration(
            clinic_id=clinic,
            expected_version=0,
            content=ConfigurationContent(
                display_name="Clínica Sintética", queue_quotas={"ai-batch": 60}
            ),
        )
        lines.append("legitimate {'ai-batch': 60} -> accepted")
    ok = all("rejected" in line for line in lines[:-1]) and "accepted" in lines[-1]
    receipt = {
        "provenance": _provenance(
            "uv run --frozen --no-sync --no-env-file python "
            "tests/core/fairness_probe.py quota-forge"
        ),
        "lines": lines,
        "exit_code": 0 if ok else 1,
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "quota-forge.txt").write_text(
        json.dumps(receipt, indent=2) + "\n" + "\n".join(lines) + "\n"
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def main() -> int:
    """Dispatch one QA scenario by name."""
    scenarios = {
        "fair": scenario_fair,
        "redis-down": scenario_redis_down,
        "quota-forge": scenario_quota_forge,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in scenarios:
        sys.stderr.write(f"usage: fairness_probe {'|'.join(scenarios)}\n")
        return 2
    return scenarios[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
