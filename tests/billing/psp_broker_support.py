"""Event-driven, process-group-owned startup for the required PSP broker gate."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from threading import Event, Thread
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path


@contextmanager
def event_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None,
    ready_line: str,
) -> Iterator[None]:
    """Subscribe to stdout before startup; bound readiness and process shutdown."""
    ready = Event()
    process = subprocess.Popen(  # noqa: S603 - fixed caller-owned argv, no shell
        argv,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def subscribe() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if ready_line in line:
                ready.set()
        ready.set()

    reader = Thread(target=subscribe, daemon=True)
    reader.start()
    try:
        assert ready.wait(timeout=90), "process did not publish its readiness event"
        assert process.poll() is None, "process exited before readiness"
        yield
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=15)
        # Also close any prefork descendants that outlived the main worker.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        reader.join(timeout=10)
        assert not reader.is_alive()
        assert process.stdout is not None
        process.stdout.close()


@dataclass(frozen=True, slots=True)
class GateBroker:
    """The test owns the single local Redis endpoint for this gate."""

    url: str


@pytest.fixture
def broker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GateBroker]:
    """Require Redis and await its exact ready event without a polling loop."""
    external = os.environ.get("CLINIC_PSP_GATE_BROKER_URL")
    if external is not None:
        endpoint = urlsplit(external)
        assert endpoint.scheme == "redis"
        assert endpoint.hostname == "127.0.0.1"
        assert endpoint.port is not None
        assert endpoint.port != 6379
        assert endpoint.username is None
        assert endpoint.password is None
        assert endpoint.path == "/0"
        assert not endpoint.query
        assert not endpoint.fragment
        yield GateBroker(external)
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.fail("required PSP broker gate needs redis-server")
    root = tmp_path_factory.mktemp("psp-redis")
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
            "--protected-mode",
            "yes",
        ),
        cwd=root,
        environment=None,
        ready_line="Ready to accept connections",
    ):
        yield GateBroker(f"redis://127.0.0.1:{port}/0")
