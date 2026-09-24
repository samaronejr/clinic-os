"""Spawn one process-group payload behind a journal release barrier."""

from __future__ import annotations

import hashlib
import os
import select
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never, final

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    raw_sha256,
)
from ops.testing.isolation_controller_kernel import ChildIdentity, WaitResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

READY_FD = 3
RELEASE_FD = 4
START_TICKS_INDEX = 21
LAUNCHER = Path(__file__).with_name("review_stage_launcher.py")


@final
@dataclass(slots=True)
class BarrierProcess:
    """Own one blocked process group and its private barrier descriptors."""

    pid: int
    identity: ChildIdentity
    ready_descriptor: int | None
    release_descriptor: int | None
    released: bool = False
    reaped: bool = False

    def release(self) -> None:
        """Release the payload exactly once after its identity is journaled."""
        if self.released or self.release_descriptor is None:
            _fail("barrier payload was already released")
        os.write(self.release_descriptor, b"G")
        os.close(self.release_descriptor)
        self.release_descriptor = None
        self.released = True

    def wait(self, timeout_seconds: int) -> WaitResult:
        """Observe a bounded terminal result and reap the process group."""
        deadline = time.monotonic() + timeout_seconds
        status: int | None = None
        while time.monotonic() < deadline:
            waited, candidate = os.waitpid(self.pid, os.WNOHANG)
            if waited == self.pid:
                status = candidate
                break
            time.sleep(0.02)
        timed_out = status is None
        if status is None:
            _signal_group(self.pid, signal.SIGTERM)
            status = _bounded_reap(self.pid, 5)
        if status is None:
            _signal_group(self.pid, signal.SIGKILL)
            _, status = os.waitpid(self.pid, 0)
        self.reaped = True
        exit_code = os.waitstatus_to_exitcode(status)
        signal_number = -exit_code if exit_code < 0 else None
        code = exit_code if exit_code >= 0 else None
        outcome: JsonObject = {
            "exit_code": code,
            "signal": signal_number,
            "timed_out": timed_out,
        }
        digest = raw_sha256(canonical_bytes(outcome))
        return WaitResult(code, signal_number, timed_out, digest)

    def close(self) -> None:
        """Close barriers and kill/reap an unfinished child without ambiguity."""
        for name in ("ready_descriptor", "release_descriptor"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)
        if not self.reaped:
            _signal_group(self.pid, signal.SIGKILL)
            with suppress(ChildProcessError):
                _ = os.waitpid(self.pid, 0)
            self.reaped = True


def start_barrier_process(
    payload: Sequence[str],
    environment: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> BarrierProcess:
    """Spawn and return only after the child proves it is blocked at ready."""
    if not payload or not Path(payload[0]).is_absolute():
        _fail("barrier payload does not begin with an absolute executable")
    ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
    release_read, release_write = os.pipe2(os.O_CLOEXEC)
    stdout = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    stderr = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    actions = (
        (os.POSIX_SPAWN_DUP2, ready_write, READY_FD),
        (os.POSIX_SPAWN_DUP2, release_read, RELEASE_FD),
        (os.POSIX_SPAWN_DUP2, stdout, 1),
        (os.POSIX_SPAWN_DUP2, stderr, 2),
        (os.POSIX_SPAWN_CLOSE, ready_read),
        (os.POSIX_SPAWN_CLOSE, release_write),
    )
    argv = barrier_argv(payload)
    try:
        pid = os.posix_spawn(
            sys.executable,
            argv,
            dict(environment),
            file_actions=actions,
        )
    finally:
        for descriptor in (ready_write, release_read, stdout, stderr):
            os.close(descriptor)
    if not select.select([ready_read], [], [], 5)[0] or os.read(ready_read, 1) != b"R":
        os.close(ready_read)
        os.close(release_write)
        _signal_group(pid, signal.SIGKILL)
        _ = os.waitpid(pid, 0)
        _fail("barrier child did not reach readiness")
    identity = ChildIdentity(pid, pid, _start_ticks(pid))
    return BarrierProcess(pid, identity, ready_read, release_write)


def barrier_argv(payload: Sequence[str]) -> tuple[str, ...]:
    """Return the exact process argv used before the payload barrier releases."""
    return (
        sys.executable,
        "-I",
        "-P",
        "-B",
        str(LAUNCHER),
        "--ready-fd",
        str(READY_FD),
        "--release-fd",
        str(RELEASE_FD),
        "--parent-pid",
        str(os.getpid()),
        *payload,
    )


def barrier_argv_sha256(payload: Sequence[str]) -> str:
    """Hash the exact null-delimited pre-release process argv."""
    return hashlib.sha256(
        b"".join(item.encode() + b"\0" for item in barrier_argv(payload))
    ).hexdigest()


def _bounded_reap(pid: int, seconds: int) -> int | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.02)
    return None


def _signal_group(pid: int, value: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, value)


def _start_ticks(pid: int) -> int:
    fields = (Path("/proc") / str(pid) / "stat").read_text().split()
    if len(fields) <= START_TICKS_INDEX:
        _fail("barrier child process identity is incomplete")
    return int(fields[START_TICKS_INDEX])


def _fail(message: str) -> Never:
    raise IsolationError(message)
