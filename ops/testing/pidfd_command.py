"""Interrupt-safe, bounded local subprocesses: signal, observe and reap by pidfd."""

from __future__ import annotations

import math
import os
import select
import signal
import subprocess
import time
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from threading import current_thread, main_thread
from typing import TYPE_CHECKING

from . import pidfd_spawn

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import BinaryIO

CLEANUP_SECONDS = 3
GROUP = 1 << 2  # Linux PIDFD_SIGNAL_PROCESS_GROUP.


@contextmanager
def waitable_children() -> Iterator[str]:
    """Establish normal child-wait semantics and restore the inherited setting."""
    prior = signal.getsignal(signal.SIGCHLD)
    if prior is None:
        message = "unrestorable SIGCHLD disposition"
        raise RuntimeError(message)
    if current_thread() is not main_thread():
        # signal.signal is process-wide and may only be set by the main thread.
        # A caller on another thread must already be under normal wait semantics.
        if prior != signal.SIG_DFL:
            message = "non-main-thread spawn requires default SIGCHLD"
            raise RuntimeError(message)
        yield "default"
        return
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        yield (
            "default"
            if prior == signal.SIG_DFL
            else "ignored"
            if prior == signal.SIG_IGN
            else "handler"
        )
    finally:
        signal.signal(signal.SIGCHLD, prior)
        if signal.getsignal(signal.SIGCHLD) != prior:
            message = "inherited SIGCHLD disposition was not restored"
            raise RuntimeError(message)


def ready(fd: int, until: float) -> bool:
    """Observe readiness of the retained pidfd without consuming wait status."""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(max(0, math.ceil((until - time.monotonic()) * 1000))))


def _status(fd: int, until: float) -> int:
    if not ready(fd, until):
        message = "owned child did not exit before its deadline"
        raise TimeoutError(message)
    status = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
    if status is None:
        message = "pidfd ready without a wait status"
        raise RuntimeError(message)
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


@dataclass(slots=True)
class Birth:
    """A single-use, preallocated owner cell, not a child-generated receipt."""

    slot: pidfd_spawn.PidfdSlot = field(default_factory=pidfd_spawn.PidfdSlot)
    returncode: int | None = None
    stdout: BinaryIO | None = None
    stderr: BinaryIO | None = None

    @property
    def fd(self) -> int:
        """Return the kernel-created descriptor, never a numeric process ID."""
        if self.slot.value < 0:
            message = "unpublished kernel pidfd"
            raise RuntimeError(message)
        return self.slot.value

    def wait(self, timeout: float) -> int:
        """Consume only this child's wait status."""
        if self.returncode is None:
            self.returncode = _status(self.fd, time.monotonic() + timeout)
        return self.returncode

    @property
    def pid(self) -> int:
        """For observations only; never use the number to signal or recover."""
        with Path(f"/proc/self/fdinfo/{self.fd}").open(encoding="ascii") as info:
            for line in info:
                if line.startswith("Pid:"):
                    return int(line.partition(":")[2])
        message = "missing pidfd process identity"
        raise RuntimeError(message)

    def close(self, *, group: bool) -> None:
        """Handle a lost spawn return or a normal exit through the same slot."""
        fd = self.slot.value
        if fd < 0:
            return
        try:
            if self.returncode is None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(
                        fd, signal.SIGKILL, None, GROUP if group else 0
                    )
                self.wait(CLEANUP_SECONDS)
        finally:
            self.slot.value = -1
            os.close(fd)


@cache
def _require_group_signal() -> None:
    """Probe on a private inert leader before admitting a workload or its children."""
    probe = Birth()
    with waitable_children(), Path(os.devnull).open("r+b", buffering=0) as devnull:
        fds = tuple((devnull.fileno(), target) for target in range(3))
        try:
            pidfd_spawn.acquire(probe.slot, pidfd_spawn.Launch(("/usr/bin/true",), fds))
            signal.pidfd_send_signal(probe.fd, 0, None, GROUP)
        finally:
            # The capability being tested is not needed to clean up this probe.
            probe.close(group=False)


@contextmanager
def created(plan: pidfd_spawn.Launch) -> Iterator[Birth]:
    """Install cleanup before native creation can publish into the owner cell."""
    child = Birth()
    with waitable_children():
        if plan.session:
            _require_group_signal()
        try:
            pidfd_spawn.acquire(child.slot, plan)
            yield child
        finally:
            child.close(group=plan.session)


@contextmanager
def _memory_file(name: str) -> Iterator[BinaryIO]:
    with os.fdopen(os.memfd_create(name, os.MFD_CLOEXEC), "w+b") as stream:
        yield stream


@dataclass(frozen=True, slots=True)
class Command:
    """Bound one argv execution and choose its data channels."""

    timeout: float
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    stdin: bytes | None = None
    log: BinaryIO | None = None


def run_bytes(
    argv: Sequence[str], options: Command
) -> subprocess.CompletedProcess[bytes]:
    """Return captured output (or stream a combined log); never call Popen.kill."""
    until = time.monotonic() + options.timeout
    with ExitStack() as stack:
        stdin = stack.enter_context(_memory_file("command-stdin"))
        if options.stdin:
            stdin.write(options.stdin)
            stdin.seek(0)
        out = options.log or stack.enter_context(_memory_file("command-stdout"))
        err = options.log or stack.enter_context(_memory_file("command-stderr"))
        fds = ((stdin.fileno(), 0), (out.fileno(), 1), (err.fileno(), 2))
        with created(pidfd_spawn.Launch(argv, fds, options.cwd, options.env)) as child:
            if not ready(child.fd, until) or time.monotonic() >= until:
                raise subprocess.TimeoutExpired(argv, options.timeout)
        if child.returncode is None:
            message = "owned command left without a recorded wait status"
            raise RuntimeError(message)
        if options.log is not None:
            return subprocess.CompletedProcess(argv, child.returncode, b"", b"")
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(
            argv, child.returncode, out.read(), err.read()
        )


def run_text(
    argv: Sequence[str], options: Command, *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Text-mode counterpart to run_bytes for local fixtures."""
    raw = run_bytes(argv, options)
    result = subprocess.CompletedProcess(
        argv, raw.returncode, raw.stdout.decode(), raw.stderr.decode()
    )
    if check:
        result.check_returncode()
    return result
