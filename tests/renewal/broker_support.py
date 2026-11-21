"""Owned Redis and subprocess workers for the required integration gate."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
from config.celery import app as celery_app
from redis import Redis

from billing.psp_broker_support import event_process

if TYPE_CHECKING:
    from collections.abc import Iterator
    from subprocess import Popen
    from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUEUE = "clinic-integrations"


@pytest.fixture(scope="session")
def broker_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Use an explicitly owned endpoint or listen for a private Redis startup."""
    external = os.environ.get("CLINIC_WORKER_GATE_BROKER_URL")
    if external is not None:
        endpoint = urlsplit(external)
        assert endpoint.scheme == "redis"
        assert endpoint.hostname == "127.0.0.1"
        assert endpoint.port not in {None, 6379}
        assert endpoint.username is None
        assert endpoint.password is None
        assert endpoint.path == "/0"
        assert not endpoint.query
        assert not endpoint.fragment
        yield external
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.fail("required broker gate needs redis-server or a job-owned Redis URL")
    root = tmp_path_factory.mktemp("worker-redis")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with event_process(
        (
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
        ),
        cwd=root,
        environment=None,
        ready_line="Ready to accept connections",
    ):
        yield f"redis://127.0.0.1:{port}/0"


@pytest.fixture
def broker(broker_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Redis]:
    """Bind both the producer and event reader to this test's private broker."""
    monkeypatch.setenv("CELERY_BROKER_URL", broker_url)
    monkeypatch.setattr(celery_app, "_pool", None)
    monkeypatch.setattr(celery_app.amqp, "_producer_pool", None)
    assert celery_app.amqp.producer_pool.connections.connection.as_uri() == broker_url
    with Redis.from_url(broker_url, decode_responses=True) as redis:
        redis.flushdb()
        yield redis


def read_event(redis: Redis, key: str) -> list[object]:
    """Subscribe to a retained event; the deadline only bounds test failure."""
    event = redis.blpop(key, timeout=60)
    assert isinstance(event, (tuple, list)), f"worker did not signal {key}"
    received: object = json.loads(event[1])
    assert isinstance(received, list)
    return received


@dataclass(frozen=True, slots=True)
class Delivery:
    task_id: str
    redelivered: bool
    outcome: str


def delivery(redis: Redis, operation_id: UUID) -> Delivery:
    task_id, redelivered, outcome = read_event(redis, f"completed:{operation_id}")
    assert isinstance(task_id, str)
    assert isinstance(redelivered, bool)
    assert isinstance(outcome, str)
    return Delivery(task_id, redelivered, outcome)


@dataclass(frozen=True, slots=True)
class Worker:
    broker_url: str
    app_database_url: str
    secret_dir: Path

    @contextmanager
    def start(self, **extra_env: str) -> Iterator[Popen[str]]:
        """Start the real prefork worker with the production task registry."""
        environment = {
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "MIGRATION_DATABASE_URL": self.app_database_url,
            "CELERY_BROKER_URL": self.broker_url,
            "CLINIC_SECRET_BACKEND": "synthetic-file",
            "CLINIC_SECRET_DIR": str(self.secret_dir),
            "CLINIC_DATA_MODE": "synthetic",
            "COMMS_SYNTHETIC_CHANNELS": "email,sms,whatsapp",
            "PRESCRIPTION_SYNTHETIC_SIGNING": "true",
            "SECRET_KEY": "synthetic-worker-gate",
            "PYTHONPATH": str(PROJECT_ROOT / "tests"),
            "PATH": os.environ["PATH"],
            **extra_env,
        }
        with event_process(
            (
                sys.executable,
                "-m",
                "celery",
                "-A",
                "renewal.broker_worker",
                "worker",
                "-Q",
                QUEUE,
                "--pool=prefork",
                "--concurrency=1",
                "-l",
                "INFO",
            ),
            cwd=PROJECT_ROOT,
            environment=environment,
            ready_line='"%s ready."',
        ) as process:
            yield process


@pytest.fixture
def worker(
    broker_url: str, app_database_url: str, synthetic_secret_backend: Path
) -> Worker:
    return Worker(broker_url, app_database_url, synthetic_secret_backend)
