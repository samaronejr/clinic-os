"""Owned Linux children: launch publication, handle-only signalling and reaping."""

from __future__ import annotations

import math
import os
import select
import selectors
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never

if TYPE_CHECKING:
    from collections.abc import Iterator

CLEANUP_SECONDS = 3
_PEEK = os.WEXITED | os.WNOHANG | os.WNOWAIT
# Linux UAPI linux/pidfd.h (6.9+). Only the outer worker starts a new session.
# Signal scope is the group bound to the original pidfd, not the numeric PGID.
_PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2


def _bad_receipt() -> Never:
    message = "missing or malformed child ownership receipt"
    raise RuntimeError(message)


@contextmanager
def waitable_children() -> Iterator[str]:
    inherited = signal.getsignal(signal.SIGCHLD)
    if inherited is None:
        message = "cannot restore an unknown SIGCHLD disposition"
        raise RuntimeError(message)
    # Install even for DFL: an inherited SA_NOCLDWAIT must not survive.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        yield (
            "ignored"
            if inherited == signal.SIG_IGN
            else "default"
            if inherited == signal.SIG_DFL
            else "handler"
        )
    finally:
        signal.signal(signal.SIGCHLD, inherited)
        if signal.getsignal(signal.SIGCHLD) != inherited:
            message = "inherited SIGCHLD disposition was not restored"
            raise RuntimeError(message)


def exited(fd: int, until: float) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(max(0, math.ceil((until - time.monotonic()) * 1000))))


def worker_output(proc: subprocess.Popen[bytes], fd: int, until: float) -> bytes:
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
    proc: subprocess.Popen[bytes] | None,
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


def reap_tree(proc: subprocess.Popen[bytes] | None, fd: int | None = None) -> None:
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
) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    with waitable_children(), _launch(kind, until) as child:
        yield child


@contextmanager
def _launch(
    kind: Literal["make", "worker"], until: float
) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    """EOF aborts an unpublished launch. Only a validated pidfd permits exec."""
    proc: subprocess.Popen[bytes] | None = None
    fd: int | None = None
    admitted = False
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        try:
            try:
                proc = subprocess.Popen(
                    (sys.executable, "-I", "-S", "tests/infra/posture_spawn_gate.py"),
                    cwd=Path(__file__).resolve().parents[2],
                    env=os.environ
                    | {
                        "CLINIC_TEST_GATE_FD": str(child.fileno()),
                        "CLINIC_TEST_GATE_KIND": kind,
                    },
                    pass_fds=(child.fileno(),),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE if kind == "worker" else subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    start_new_session=kind == "worker",
                )
            finally:
                child.close()  # This process must never keep the peer endpoint open.
            parent.settimeout(max(0, until - time.monotonic()))
            fd = _receive(parent)
            if kind == "worker":
                # Fail closed *before* admitting a tree on an unsupported kernel.
                signal.pidfd_send_signal(fd, 0, None, _PIDFD_SIGNAL_PROCESS_GROUP)
            admitted = True  # A failed send could have delivered the byte.
            parent.sendall(b"G")
            yield proc, fd
        finally:
            try:
                if fd is None:
                    # The constructor may have forked and then raised before
                    # returning Popen. Recover only through its private channel.
                    parent.settimeout(CLEANUP_SECONDS)
                    with suppress(OSError, RuntimeError):
                        fd = _receive(parent)
                parent.close()  # No receipt/release: the gate exits without exec.
                if kind == "make":
                    reap_tree(proc, fd)
                else:
                    reap_owned(proc, fd, session=admitted)
            finally:
                if fd is not None:
                    os.close(fd)
                if proc is not None:
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
