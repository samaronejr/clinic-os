"""Owned Linux children: launch publication, handle-only signalling and reaping."""

from __future__ import annotations

import math
import os
import select
import selectors
import signal
import socket
import sys
import time
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never

from ops.testing import pidfd_spawn
from ops.testing.pidfd_command import Birth, waitable_children

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator
    from typing import BinaryIO

CLEANUP_SECONDS = 3
_PEEK = os.WEXITED | os.WNOHANG | os.WNOWAIT
__all__ = ["waitable_children"]
# Linux UAPI linux/pidfd.h (6.9+). Only the outer worker starts a new session.
# Signal scope is the group bound to the original pidfd, not the numeric PGID.
_PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2


def _bad_receipt() -> Never:
    message = "missing or malformed child ownership receipt"
    raise RuntimeError(message)


def exited(fd: int, until: float) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(max(0, math.ceil((until - time.monotonic()) * 1000))))


def worker_output(proc: Birth, fd: int, until: float) -> bytes:
    """Drain both pipes without letting communicate()/wait() reap the leader."""
    assert proc.stdout is not None
    assert proc.stderr is not None
    data = bytearray()
    with selectors.DefaultSelector() as watcher:
        for stream in (proc.stdout, proc.stderr):
            watcher.register(stream, selectors.EVENT_READ)
        while watcher.get_map():
            ready = watcher.select(max(0, until - time.monotonic()))
            if not ready or time.monotonic() >= until:
                message = "posture worker exceeded its outer deadline"
                raise TimeoutError(message)
            for key, _events in ready:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    watcher.unregister(key.fd)
                elif key.fileobj is proc.stdout:
                    data.extend(chunk)
    if not exited(fd, until):
        message = "posture worker did not exit before its outer deadline"
        raise TimeoutError(message)
    status = os.waitid(os.P_PIDFD, fd, _PEEK)
    if status is None or status.si_code != os.CLD_EXITED or status.si_status:
        message = "posture trace worker failed"
        raise RuntimeError(message)
    return bytes(data)


def finish(fd: int, until: float, *, session: bool = False) -> int:
    # Even if another waiter consumed the status, a retained pidfd cannot
    # retarget a replacement. ESRCH is never retried by number.
    with suppress(ProcessLookupError):
        signal.pidfd_send_signal(
            fd, signal.SIGKILL, None, _PIDFD_SIGNAL_PROCESS_GROUP if session else 0
        )
    if not exited(fd, until):
        message = "owned child did not exit within cleanup budget"
        raise TimeoutError(message)
    status = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
    if status is None:
        message = "pidfd readiness without child exit"
        raise RuntimeError(message)
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


@contextmanager
def direct_child(pid: int) -> Iterator[int]:
    """Use only in the dedicated subreaper: authenticate an adopted child."""
    fd = os.pidfd_open(pid)
    try:
        os.waitid(os.P_PIDFD, fd, _PEEK)  # ECHILD: no authority to signal this fd.
        yield fd
    finally:
        os.close(fd)


def drain_adopted(until: float) -> None:
    # No other jobs are spawned in this dedicated worker. Descendants that
    # outlive their parent become our direct children, even if they changed PGID.
    children = Path("/proc/thread-self/children")
    while time.monotonic() < until:
        try:
            os.waitid(os.P_ALL, 0, _PEEK)
        except ChildProcessError:
            return
        for token in children.read_text().split():
            with (
                suppress(ChildProcessError, ProcessLookupError),
                direct_child(int(token)) as fd,
            ):
                finish(fd, until)
    message = "posture descendant cleanup exceeded its separate deadline"
    raise TimeoutError(message)


def reap_owned(
    proc: subprocess.Popen[bytes] | Birth | None,
    fd: int | None = None,
    *,
    session: bool = False,
) -> None:
    if fd is None:
        if proc is None or proc.returncode is not None:
            return
        # No handle means no signal. An unreleased gate will exit on EOF.
        proc.wait(timeout=CLEANUP_SECONDS)
        return
    try:
        code = finish(fd, time.monotonic() + CLEANUP_SECONDS, session=session)
    except ChildProcessError:
        if proc is None or proc.returncode is None:
            raise
    else:
        if proc is not None:
            proc.returncode = code


def reap_tree(
    proc: subprocess.Popen[bytes] | Birth | None, fd: int | None = None
) -> None:
    # An already-reaped, handleless value conveys no ownership at all.
    if fd is None and proc is not None and proc.returncode is not None:
        return
    try:
        reap_owned(proc, fd)
    finally:
        drain_adopted(time.monotonic() + CLEANUP_SECONDS)


def _receive(channel: socket.socket) -> int:
    message, descriptors, flags, _ = socket.recv_fds(
        channel, 1, 1, socket.MSG_CMSG_CLOEXEC
    )
    try:
        if (
            message != b"P"
            or len(descriptors) != 1
            or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        ):
            _bad_receipt()
        (fd,) = descriptors
        os.waitid(os.P_PIDFD, fd, _PEEK)
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    return fd


@contextmanager
def owned_spawn(
    kind: Literal["make", "worker"], until: float
) -> Iterator[tuple[Birth, int]]:
    with waitable_children(), _launch(kind, until) as child:
        yield child


def _capture_pipe(readers: ExitStack, writers: ExitStack) -> tuple[BinaryIO, int]:
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    writers.callback(os.close, write_fd)
    return readers.enter_context(os.fdopen(read_fd, "rb", buffering=0)), write_fd


@contextmanager
def _launch(
    kind: Literal["make", "worker"], until: float
) -> Iterator[tuple[Birth, int]]:
    """Admission is a user-space handshake; custody is not. It exists at clone."""
    proc = Birth()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receipt: int | None = None
    admitted = False
    with parent, child, ExitStack() as readers:
        try:
            with ExitStack() as writers:
                null = writers.enter_context(Path(os.devnull).open("r+b", buffering=0))
                output = null.fileno()
                if kind == "worker":
                    proc.stdout, output = _capture_pipe(readers, writers)
                proc.stderr, errors = _capture_pipe(readers, writers)
                plan = pidfd_spawn.Launch(
                    (sys.executable, "-I", "-S", "tests/infra/posture_spawn_gate.py"),
                    ((null.fileno(), 0), (output, 1), (errors, 2), (child.fileno(), 3)),
                    cwd=Path(__file__).resolve().parents[2],
                    env=os.environ
                    | {"CLINIC_TEST_GATE_FD": "3", "CLINIC_TEST_GATE_KIND": kind},
                    session=kind == "worker",
                )
                pidfd_spawn.acquire(proc.slot, plan)
            child.close()
            parent.settimeout(max(0, until - time.monotonic()))
            receipt = _receive(parent)
            if not os.path.samestat(os.fstat(receipt), os.fstat(proc.fd)):
                _bad_receipt()
            if kind == "worker":
                signal.pidfd_send_signal(proc.fd, 0, None, _PIDFD_SIGNAL_PROCESS_GROUP)
            admitted = True
            parent.sendall(b"G")
            yield proc, proc.fd
        finally:
            # Reclaim through the parent slot before closing channels: even
            # an error while shutting a socket cannot bypass the kernel wait.
            try:
                if proc.slot.value >= 0:
                    if kind == "make":
                        reap_tree(proc, proc.fd)
                    else:
                        reap_owned(proc, proc.fd, session=admitted)
            finally:
                try:
                    if receipt is not None:
                        os.close(receipt)
                finally:
                    if proc.slot.value >= 0:
                        os.close(proc.fd)
                        proc.slot.value = -1
