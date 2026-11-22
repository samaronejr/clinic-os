"""Durable supervisor that owns the Gunicorn master for the browser session.

The thin shell execs this module. The supervisor never runs product code: it
serves one authenticated request protocol for its journal-bound controller
child, performs the journaled two-phase fork/exec of the Gunicorn master into
its own process group, and guarantees a bounded TERM/wait/KILL/reap teardown so
no master, worker, or listener can outlive the session.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import math
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Final, Never

import rfc8785

START_MASTER: Final = "start-master"
STOP_MASTER: Final = "stop-master"
MASTER_STARTED: Final = "master-started"
MASTER_STOPPED: Final = "master-stopped"
REQUEST_KEYS: Final = frozenset({"kind", "port", "schema_version", "sequence", "token"})
RESPONSE_KEYS: Final = frozenset(
    {"kind", "pgid", "pid", "schema_version", "sequence", "worker_count"}
)
WORKER_COUNT: Final = 2
REQUEST_TIMEOUT_SECONDS: Final = 30
MAX_MESSAGE_BYTES: Final = 4096
TERMINATION_GRACE_SECONDS: Final = 10
# Linux TCP_TIMEWAIT_LEN is 60 seconds. This budget is for retired
# connections only; it does not extend either process-group grace period.
PORT_RELEASE_SECONDS: Final = 65
TCP_TIME_WAIT: Final = "06"
TFD_TIMER_ABSTIME: Final = 1
PR_SET_CHILD_SUBREAPER: Final = 36
PROTECTED_DATABASE_PORT: Final = 5432
MAX_PORT: Final = 65535


class SupervisorError(RuntimeError):
    """Reject an unauthenticated or malformed supervisor request."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying supervisor failure."""
        super().__init__(f"browser supervisor rejected: {reason}")


def _fail(reason: str) -> Never:
    raise SupervisorError(reason)


def build_request(kind: str, port: int, sequence: int, token: str) -> bytes:
    """Build one canonical authenticated supervisor request."""
    if kind not in {START_MASTER, STOP_MASTER}:
        _fail(f"{kind} is not a supported supervisor request")
    if kind == START_MASTER and (
        port == PROTECTED_DATABASE_PORT or not 1 <= port <= MAX_PORT
    ):
        _fail("requested port is invalid or protected")
    if sequence < 0 or not token:
        _fail("request sequence or token is invalid")
    return (
        rfc8785.dumps(
            {
                "kind": kind,
                "port": port,
                "schema_version": 1,
                "sequence": sequence,
                "token": token,
            }
        )
        + b"\n"
    )


def parse_request(raw: bytes, token: str, sequence: int) -> tuple[str, int]:
    """Authenticate one bounded request against this session's token order."""
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        _fail("request is empty or exceeds the bounded size")
    document: object = json.loads(raw)
    if not isinstance(document, dict) or set(document) != set(REQUEST_KEYS):
        _fail("request has the wrong closed key set")
    if document.get("schema_version") != 1:
        _fail("request schema version is not 1")
    if document.get("token") != token or document.get("sequence") != sequence:
        _fail("request is not authenticated for this session")
    kind = document.get("kind")
    port = document.get("port")
    if kind not in {START_MASTER, STOP_MASTER} or not isinstance(port, int):
        _fail("request kind or port is invalid")
    return kind, port


def build_response(kind: str, pid: int, pgid: int, sequence: int) -> bytes:
    """Build the fixed supervisor handoff returned to the controller."""
    return (
        rfc8785.dumps(
            {
                "kind": kind,
                "pgid": pgid,
                "pid": pid,
                "schema_version": 1,
                "sequence": sequence,
                "worker_count": WORKER_COUNT,
            }
        )
        + b"\n"
    )


def parse_response(raw: bytes, kind: str, sequence: int) -> tuple[int, int]:
    """Return the master identity only from a well-formed fixed handoff."""
    document: object = json.loads(raw)
    if not isinstance(document, dict) or set(document) != set(RESPONSE_KEYS):
        _fail("response has the wrong closed key set")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != kind
        or document.get("sequence") != sequence
        or document.get("worker_count") != WORKER_COUNT
    ):
        _fail("response is not the fixed supervisor handoff")
    pid = document.get("pid")
    pgid = document.get("pgid")
    if not isinstance(pid, int) or not isinstance(pgid, int):
        _fail("response master identity is invalid")
    return pid, pgid


def supervised_argv(interpreter: Path, listener_fd: int, access_log: Path) -> list[str]:
    """Return the frozen two-worker preloaded Gunicorn argv."""
    if not interpreter.is_absolute() or not os.access(interpreter, os.X_OK):
        _fail("supervised interpreter is not an executable absolute path")
    return [
        str(interpreter),
        "-m",
        "gunicorn",
        "--bind",
        f"fd://{listener_fd}",
        f"--workers={WORKER_COUNT}",
        "--worker-class=sync",
        "--threads=1",
        f"--timeout={REQUEST_TIMEOUT_SECONDS}",
        "--preload",
        "--access-logfile",
        str(access_log),
        # ADR-014: method + status + duration only; no request line or peer.
        "--access-logformat",
        "%(m)s %(s)s %(D)s",
        "--error-logfile",
        "-",
        "config.wsgi:application",
    ]


def _require_subreaper() -> None:
    # Orphaned Gunicorn workers must be reparented here, not to PID 1.
    if ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        _fail("could not adopt orphaned Gunicorn workers")


def _adopted_member(pgid: int) -> int | None:
    # After the master exits, a surviving process tree in the group has
    # at least one root reparented to this subreaper.
    identity = [str(os.getpid()).encode(), str(pgid).encode()]
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if stat[1:3] == identity:
            return int(entry.name)
    return None


def _wait_for_exit(pid: int, deadline: float) -> bool:
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(
            poller.poll(max(0, math.ceil((deadline - time.monotonic()) * 1000)))
        )
    finally:
        os.close(fd)


def _wait_for_group(process: subprocess.Popen[bytes], deadline: float) -> bool:
    while True:
        if process.poll() is None:
            pid = process.pid
        else:
            adopted = _adopted_member(process.pid)
            if adopted is None:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    return True
                # An adoption raced the process table scan. Never report an
                # empty group without the kernel confirming it.
                if time.monotonic() >= deadline:
                    return False
                continue
            pid = adopted
        if not _wait_for_exit(pid, deadline):
            return False
        if pid != process.pid:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Signal the owned session's process group even after its leader exits."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        # A live or unreaped owned child pins the group identity. Do not
        # signal a numeric PGID after the entire owned group has been reaped.
        if process.poll() is None or _adopted_member(process.pid) is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signum)
        if _wait_for_group(process, time.monotonic() + TERMINATION_GRACE_SECONDS):
            return
    _fail("Gunicorn process group or listener still owned after teardown deadline")


def _time_wait_seconds(port: int) -> float | None:
    # /proc exposes the remaining kernel timer in clock ticks. Only consider
    # retired TCP sockets at the address this IPv4 listener used.
    latest: int | None = None
    with Path("/proc/self/net/tcp").open(encoding="ascii") as table:
        next(table)
        for line in table:
            fields = line.split()
            host, local_port = fields[1].split(":")
            if (
                fields[3] == TCP_TIME_WAIT
                and int(local_port, 16) == port
                and host in {"0100007F", "00000000"}
            ):
                _, ticks = fields[5].split(":")
                latest = max(latest or 0, int(ticks, 16))
    # Round up the exported tick count rather than waking before expiry.
    return None if latest is None else (latest + 1) / os.sysconf("SC_CLK_TCK")


class _Timespec(ctypes.Structure):
    _fields_ = (("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long))


class _TimerSpec(ctypes.Structure):
    _fields_ = (("interval", _Timespec), ("value", _Timespec))


def _wait_for_tcp_expiry(until: float) -> None:
    # Python 3.12 does not expose timerfd; wait for the observed kernel
    # expiry on a monotonic timer FD rather than periodically probing.
    library = ctypes.CDLL(None, use_errno=True)
    fd = int(library.timerfd_create(time.CLOCK_MONOTONIC, os.O_CLOEXEC))
    if fd < 0:
        _fail("could not open a Gunicorn port release timer")
    try:
        seconds = int(until)
        expiration = _Timespec(seconds, int((until - seconds) * 1e9))
        timer = _TimerSpec(_Timespec(), expiration)
        armed = library.timerfd_settime(
            fd, TFD_TIMER_ABSTIME, ctypes.byref(timer), None
        )
        if armed != 0:
            _fail("could not arm the Gunicorn port release timer")
        os.read(fd, 8)
    finally:
        os.close(fd)


def require_released_port(port: int) -> None:
    """Prove an ordinary caller can bind after the owned listener closes."""
    deadline = time.monotonic() + PORT_RELEASE_SECONDS
    while (error := _ordinary_bind_error(port)) is not None:
        if error.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
            break
        try:
            remaining = _time_wait_seconds(port)
        except OSError as inspect_error:
            message = "could not inspect Gunicorn port release state"
            raise SupervisorError(message) from inspect_error
        if remaining is None:
            # A retired socket can expire between the bind and the table read.
            # Check once against the observed state; a live holder still fails.
            error = _ordinary_bind_error(port)
            break
        _wait_for_tcp_expiry(min(deadline, time.monotonic() + remaining))
    if error is not None:
        message = "Gunicorn listener port was not released"
        raise SupervisorError(message) from error


def _ordinary_bind_error(port: int) -> OSError | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        return error
    return None


class SupervisedMaster:
    """Own one Gunicorn master process group for the session lifetime."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        """Retain the forked master and its process group."""
        self.process = process
        # Popen completes setsid() before returning, even if exec exits fast.
        self.pgid = process.pid
        self._terminated = False

    @property
    def pid(self) -> int:
        """Return the supervised master process identifier."""
        return self.process.pid

    def worker_pids(self) -> tuple[int, ...]:
        """Return the direct worker children observed for the master."""
        children = Path(f"/proc/{self.pid}/task/{self.pid}/children")
        if not children.exists():
            return ()
        return tuple(int(item) for item in children.read_text().split())

    def terminate(self) -> None:
        """Terminate the whole group with a bounded TERM then KILL and reap."""
        if self._terminated:
            return
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
        )
        try:
            _terminate_group(self.process)
            self._terminated = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def start_master(
    argv: list[str],
    environment: dict[str, str],
    log: Path,
    *,
    owner: list[SupervisedMaster],
    pass_fds: tuple[int, ...] = (),
) -> SupervisedMaster:
    """Perform the journaled second phase: exec the master in a new group.

    SIGINT/SIGTERM are blocked for the whole spawn→publish interval, so a
    cancellation can never land while a live spawned process is unowned:
    the child restores the inherited mask before exec, the handle is
    published into ``owner``, and only then is the mask restored — a
    pending cancellation lands inside the protected region, where the
    group is reaped before the exception propagates. Any other exception
    between the spawn and the completed handoff reaps the same way.
    """
    _require_subreaper()
    process: subprocess.Popen[bytes] | None = None
    master: SupervisedMaster | None = None
    previous_mask = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        {signal.SIGINT, signal.SIGTERM},
    )
    try:
        with log.open("wb") as stream:
            process = subprocess.Popen(  # noqa: S603 - frozen argv, resolved interpreter.
                argv,
                env=environment,
                pass_fds=pass_fds,
                start_new_session=True,
                # The child must not inherit the blocked mask: restore it
                # after fork, before exec, so the master stays killable.
                preexec_fn=lambda: signal.pthread_sigmask(  # noqa: PLW1509 - single-threaded spawn; the hook is one sigmask call.
                    signal.SIG_SETMASK,
                    previous_mask,
                ),
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        master = SupervisedMaster(process)
        owner.append(master)
        # Ownership is published; a deferred cancellation now lands inside
        # this protected region and reaps the group before propagating.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return master  # noqa: TRY300 - the return boundary is where a deferred signal is delivered; it must stay inside the protected region.
    except BaseException:
        if master is not None:
            master.terminate()
        elif process is not None:
            _terminate_group(process)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


CONTROLLER_MODULE: Final = "ops.testing.browser_server_controller"


def main() -> int:
    """Fork the journal-bound controller child and reap it on every exit."""
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    interpreter = sys.executable
    pid = os.fork()
    if pid == 0:
        parent.close()
        os.environ["CLINIC_BROWSER_SUPERVISOR_FD"] = str(child.fileno())
        child.set_inheritable(True)
        os.execv(  # noqa: S606 - fixed interpreter and closed module argv, never a shell.
            interpreter,
            [interpreter, "-m", CONTROLLER_MODULE, *sys.argv[1:]],
        )
        raise SystemExit(127)
    child.close()
    try:
        _, status = os.waitpid(pid, 0)
    finally:
        with contextlib.suppress(OSError):
            parent.close()
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
