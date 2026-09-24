"""Real broker/worker delivery proof for the renewal release candidate.

Unlike the in-process integration boundary tests, these cases dispatch
through a real task-owned Redis broker into a separately spawned Celery
worker that connects to PostgreSQL as ``clinic_app``. The whole module
requires the dedicated gate environment: ``CLINIC_BROKER_GATE=required``
plus a ``redis-server`` binary. Outside the gate it skips, and the gate job
fails loudly when a prerequisite is missing rather than reporting a
successful run with skipped evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.adapters import CallbackAuthenticationError
from apps.comms.models import AppointmentReminder, IntegrationOperation
from apps.comms.tasks import (
    dispatch_due_reminders,
    recover_pending_operations,
)
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    receive_provider_callback,
    register_callback_authenticator,
)
from apps.intake.contacts import (
    enqueue_automated_message,
    save_contact_destination,
    set_purpose_channel,
    verify_contact,
)
from apps.tenancy.db import tenant_context
from config.celery import app as celery_app
from django.db import connection, transaction

from patient_service_support import runtime_role
from renewal.test_integration_boundary import SECRET, SyntheticAuthenticator
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from io import BufferedWriter

    from pytest_django.fixtures import SettingsWrapper

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("CLINIC_BROKER_GATE", "") != "required",
        reason="requires the dedicated broker gate (CLINIC_BROKER_GATE=required)",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GATE_ENV_VAR = "CLINIC_BROKER_GATE"
POLL_INTERVAL = 0.25
POLL_TIMEOUT = 60.0
WORKER_READY_TIMEOUT = 90.0
CHANNEL = "sms"
PURPOSE = "appointment_reminder"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(predicate: Callable[[], bool], timeout: float = POLL_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL)
    return False


class _Broker:
    """One task-owned Redis process on a unique loopback port."""

    def __init__(self, *, binary: str, root: Path, port: int) -> None:
        self.url = f"redis://127.0.0.1:{port}/0"
        self._argv = (
            binary,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(root),
            "--protected-mode",
            "yes",
        )
        self._port = port
        self._log_path = root / "redis.log"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        assert self._process is None
        log = self._log_path.open("ab")
        self._process = subprocess.Popen(  # noqa: S603 - fixed binary, closed argv
            self._argv, stdout=log, stderr=subprocess.STDOUT
        )
        log.close()
        cli = shutil.which("redis-cli")
        if cli is not None:
            port = self._port
            ready = _wait_for(
                lambda: (
                    subprocess.run(  # noqa: S603 - fixed binary, closed argv
                        (cli, "-h", "127.0.0.1", "-p", str(port), "ping"),
                        capture_output=True,
                        check=False,
                    ).returncode
                    == 0
                ),
                timeout=15.0,
            )
            if not ready:
                self.stop()
                pytest.fail("task-owned redis-server did not become ready")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None


@pytest.fixture(scope="session")
def broker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Broker]:
    """Run one task-owned Redis instance on a unique loopback port."""
    binary = shutil.which("redis-server")
    if binary is None:
        if os.environ.get(GATE_ENV_VAR) == "required":
            pytest.fail("broker gate requires the redis-server binary")
        pytest.skip("redis-server is not installed")
    handle = _Broker(
        binary=binary,
        root=tmp_path_factory.mktemp("redis"),
        port=_free_port(),
    )
    handle.start()
    yield handle
    handle.stop()


@pytest.fixture(autouse=True)
def _bind_broker(broker: _Broker) -> Iterator[None]:
    """Point the test-side Celery app at the task-owned broker."""
    previous_url = celery_app.conf.broker_url
    previous_retry = celery_app.conf.task_publish_retry
    celery_app.conf.update(
        CELERY_BROKER_URL=broker.url,
        CELERY_TASK_PUBLISH_RETRY=False,
    )
    yield
    celery_app.conf.update(
        CELERY_BROKER_URL=previous_url,
        CELERY_TASK_PUBLISH_RETRY=previous_retry,
    )


class _Worker:
    """One spawned Celery worker bound to the test database as clinic_app."""

    def __init__(
        self,
        *,
        broker_url: str,
        app_dsn: str,
        secret_dir: Path,
        log_path: Path,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._env = {
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "MIGRATION_DATABASE_URL": app_dsn,
            "CELERY_BROKER_URL": broker_url,
            "CLINIC_SECRET_BACKEND": "synthetic-file",
            "CLINIC_SECRET_DIR": str(secret_dir),
            "COMMS_SYNTHETIC_CHANNELS": "email,sms,whatsapp",
            "CLINIC_DATA_MODE": "synthetic",
            "SECRET_KEY": "synthetic-worker-gate",
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - worker cwd fallback
            **(extra_env or {}),
        }
        self._log_path = log_path
        self._log: BufferedWriter | None = None
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        assert self._process is None
        assert self._log is None
        self._log = self._log_path.open("ab")
        self._process = subprocess.Popen(  # noqa: S603 - fixed module argv
            (
                sys.executable,
                "-m",
                "celery",
                "-A",
                "config.celery",
                "worker",
                "-Q",
                "clinic-integrations",
                "--concurrency=2",
                "-l",
                "INFO",
                "--hostname",
                f"rc-gate-{secrets.token_hex(4)}@%h",
            ),
            cwd=PROJECT_ROOT,
            env=self._env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + WORKER_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                assert self._log is not None
                self._log.flush()
                pytest.fail("celery worker exited during startup")
            try:
                if b"ready." in self._log_path.read_bytes()[-65536:]:
                    return
            except OSError:
                pass
            time.sleep(POLL_INTERVAL)
        self.stop()
        pytest.fail("celery worker did not signal readiness")

    def stop(self, *, kill: bool = False) -> None:
        if self._process is None:
            return
        if kill:
            self._process.send_signal(signal.SIGKILL)
        else:
            self._process.terminate()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None
        if self._log is not None:
            self._log.flush()
            self._log.close()
            self._log = None

    def close(self) -> None:
        self.stop(kill=True)


@pytest.fixture
def worker(
    broker: _Broker,
    app_database_url: str,
    synthetic_secret_backend: Path,
    tmp_path: Path,
) -> Iterator[_Worker]:
    handle = _Worker(
        broker_url=broker.url,
        app_dsn=app_database_url,
        secret_dir=synthetic_secret_backend,
        log_path=tmp_path / "worker.log",
    )
    yield handle
    handle.close()


def _contact(setup: AppointmentSetup, channel: str) -> None:
    save_contact_destination(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        channel=channel,
        destination="+5511999990001",
        expected_version=None,
    )
    verify_contact(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        channel=channel,
        expected_version=1,
    )
    set_purpose_channel(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        purpose=PURPOSE,
        channel=channel,
    )


def _operation(organization_id: UUID, operation_id: UUID) -> IntegrationOperation:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        operation = IntegrationOperation.objects.filter(pk=operation_id).first()
    assert operation is not None
    return operation


def _status(setup: AppointmentSetup, operation_id: UUID) -> str:
    return _operation(setup.organization_id, operation_id).status


def _event_count(setup: AppointmentSetup, operation_id: UUID, event: str) -> int:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        return AuditEvent.objects.filter(
            event_type=event,
            payload__request_id=str(operation_id),
        ).count()


def _make_due(setup: AppointmentSetup, operation_id: UUID) -> None:
    """Accelerate the stored schedule the same fixture-only way tests do."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        cursor.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET not_before=statement_timestamp()-interval '1 minute' "
            "WHERE id=%s",
            [operation_id],
        )


def _book_reminder(setup: AppointmentSetup, channel: str) -> UUID:
    """Book an appointment through the real services; the trigger writes the op."""
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _contact(setup, channel)
        appointment = create_synthetic_appointment(setup)
        reminder = AppointmentReminder.objects.select_related("operation").get(
            appointment=appointment
        )
        return reminder.operation_id


@pytest.fixture
def setup(rbac_graph: RbacGraph, settings: SettingsWrapper) -> AppointmentSetup:
    settings.COMMS_SYNTHETIC_CHANNELS = ("email", "sms", "whatsapp")
    return seed_appointment_setup(rbac_graph)


def test_beat_dispatch_reaches_real_worker_and_delivers_once(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    worker.start()
    operation_id = _book_reminder(setup, CHANNEL)
    _make_due(setup, operation_id)
    with runtime_role():
        assert dispatch_due_reminders() == 1
    assert _wait_for(lambda: _status(setup, operation_id) == "succeeded")
    operation = _operation(setup.organization_id, operation_id)
    assert operation.provider_reference == f"synthetic:{CHANNEL}:{operation_id}"
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


_ROLLED_BACK = "synthetic rollback"


def test_rolled_back_operation_is_never_delivered(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    worker.start()
    rolled_back = uuid4()
    with (  # noqa: PT012 - the atomic block is the unit under test
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(RuntimeError, match="synthetic rollback"),
        transaction.atomic(),
    ):
        _contact(setup, CHANNEL)
        enqueue_automated_message(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose=PURPOSE,
            provider=f"reminder-{CHANNEL}-v1",
            idempotency_key=rolled_back,
        )
        raise RuntimeError(_ROLLED_BACK)
    assert IntegrationOperation.objects.filter(idempotency_key=rolled_back).count() == 0


def test_on_commit_dispatch_reaches_worker(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    """A committed enqueue publishes on commit; the worker executes the task.

    The provider is deliberately unregistered, so the only possible outcome
    is a terminal ``adapter_unavailable`` failure recorded by the worker
    process — the pending-to-failed transition proves the task crossed the
    real broker boundary.
    """
    worker.start()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation_id = enqueue_operation(
            OperationRequest(
                channel=CHANNEL,
                provider="synthetic-unregistered",
                clinic_id=setup.clinic_id,
                subject_type="intake.patient_clinic_enrollment",
                subject_id=setup.enrollment_id,
                idempotency_key=uuid4(),
                max_attempts=2,
            )
        )
    assert _wait_for(lambda: _status(setup, operation_id) == "failed")
    operation = _operation(setup.organization_id, operation_id)
    assert operation.last_error == "adapter_unavailable"
    assert _event_count(setup, operation_id, "comms.operation.failed") == 1


def test_lost_dispatch_is_recovered_by_pending_scan(
    setup: AppointmentSetup, worker: _Worker, broker: _Broker
) -> None:
    """A queued task message lost to a broker restart is recovered.

    The broker runs without persistence, so restarting it loses the queued
    dispatch while the committed row stays pending. The pending-operation
    recovery scan re-dispatches it and the worker delivers exactly once.
    """
    operation_id = _book_reminder(setup, CHANNEL)
    _make_due(setup, operation_id)
    with runtime_role():
        dispatch_due_reminders()
    try:
        broker.stop()
        broker.start()
    finally:
        if not broker.is_running():
            broker.start()
    worker.start()
    # The queued message died with the broker; nothing delivers it alone.
    assert not _wait_for(
        lambda: _status(setup, operation_id) == "succeeded", timeout=8.0
    )
    with runtime_role():
        assert recover_pending_operations() >= 1
    assert _wait_for(lambda: _status(setup, operation_id) == "succeeded")
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


def test_duplicate_dispatch_reconciles_to_one_delivery(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    worker.start()
    operation_id = _book_reminder(setup, CHANNEL)
    _make_due(setup, operation_id)
    with runtime_role():
        dispatch_due_reminders()
    execute_operation_task.apply_async(kwargs={"operation_id": str(operation_id)})
    assert _wait_for(lambda: _status(setup, operation_id) == "succeeded")
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


def test_worker_restart_does_not_duplicate_delivery(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    operation_id = _book_reminder(setup, CHANNEL)
    _make_due(setup, operation_id)
    with runtime_role():
        dispatch_due_reminders()
    worker.start()
    assert _wait_for(lambda: _status(setup, operation_id) == "succeeded")
    worker.stop(kill=True)
    execute_operation_task.apply_async(kwargs={"operation_id": str(operation_id)})
    worker.start()
    assert _wait_for(lambda: _status(setup, operation_id) == "succeeded", timeout=15.0)
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 1


def test_revoked_contact_cancels_at_send_time(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    operation_id = _book_reminder(setup, CHANNEL)
    _make_due(setup, operation_id)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        set_purpose_channel(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            purpose=PURPOSE,
            channel=None,
        )
    with runtime_role():
        dispatch_due_reminders()
    worker.start()
    assert _wait_for(lambda: _status(setup, operation_id) == "cancelled")
    assert _event_count(setup, operation_id, "comms.operation.succeeded") == 0


def test_retry_ceiling_reaches_terminal_failure(
    setup: AppointmentSetup,
    broker: _Broker,
    app_database_url: str,
    synthetic_secret_backend: Path,
    tmp_path: Path,
) -> None:
    failing = _Worker(
        broker_url=broker.url,
        app_dsn=app_database_url,
        secret_dir=synthetic_secret_backend,
        log_path=tmp_path / "worker-failing.log",
        extra_env={"COMMS_SYNTHETIC_FAILURE_CHANNELS": CHANNEL},
    )
    try:
        failing.start()
        operation_id = _book_reminder(setup, CHANNEL)
        _make_due(setup, operation_id)
        with runtime_role():
            dispatch_due_reminders()
        # Keep a fresh dispatch queued whenever the row returns to pending:
        # racing duplicates reconcile through the claim lock without
        # incrementing the attempt count, so pacing by status is the only
        # honest way to reach the ceiling.
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            status = _status(setup, operation_id)
            if status == "failed":
                break
            if status == "pending":
                execute_operation_task.apply_async(
                    kwargs={"operation_id": str(operation_id)}
                )
            time.sleep(POLL_INTERVAL)
        assert _status(setup, operation_id) == "failed"
        operation = _operation(setup.organization_id, operation_id)
        assert operation.attempt_count == operation.max_attempts
        assert operation.last_error == "attempts_exhausted"
        assert _event_count(setup, operation_id, "comms.operation.failed") == 1
    finally:
        failing.close()


def test_forged_duplicate_and_terminal_callbacks_behave(
    setup: AppointmentSetup, worker: _Worker
) -> None:
    worker.start()
    operation_id = _book_reminder(setup, CHANNEL)
    _make_due(setup, operation_id)
    with runtime_role():
        dispatch_due_reminders()
    assert _wait_for(lambda: _status(setup, operation_id) == "succeeded")
    operation = _operation(setup.organization_id, operation_id)
    authenticator = SyntheticAuthenticator()
    authenticator.provider = operation.provider
    register_callback_authenticator(authenticator)
    body = json.dumps(
        {
            "event_id": f"synthetic-receipt-{operation_id}",
            "provider_reference": operation.provider_reference,
            "status": "delivered",
        }
    ).encode()
    with pytest.raises(CallbackAuthenticationError):
        receive_provider_callback(
            provider=operation.provider,
            headers={"x-synthetic-signature": "forged"},
            body=body,
        )
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    headers = {"x-synthetic-signature": signature}
    with runtime_role():
        first = receive_provider_callback(
            provider=operation.provider, headers=headers, body=body
        )
        second = receive_provider_callback(
            provider=operation.provider, headers=headers, body=body
        )
    assert first == "applied"
    assert second == "duplicate"
    assert _status(setup, operation_id) == "delivered"
    assert _event_count(setup, operation_id, "comms.operation.delivered") == 1
