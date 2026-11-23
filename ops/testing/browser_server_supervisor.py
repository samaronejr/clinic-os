"""Own Gunicorn from fork through bounded process-group reap and bind proof."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

import rfc8785

from ops.testing.browser_readiness import WSGI_APPLICATION
from ops.testing.diagnostic_redaction import (
    always_cleanup,
    diagnostics,
    error_details,
    preserve_cleanup_error,
    preserving_context,
)
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from threading import Thread
    from typing import BinaryIO

    from ops.testing.diagnostic_redaction import DiagnosticSink

__all__ = [
    "always_cleanup",
    "error_details",
    "preserve_cleanup_error",
    "preserving_context",
]

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
LAUNCH_SIGNALS: Final = frozenset({signal.SIGINT, signal.SIGTERM})
MAX_PORT: Final = 65535


class SupervisorError(RuntimeError):
    """Reject an unauthenticated or malformed supervisor request."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying supervisor failure."""
        super().__init__(f"browser supervisor rejected: {reason}")


class RenewalInterruptError(IsolationError):
    """Cancellation of either browser launcher by SIGINT or SIGTERM."""


def interrupted(_signum: int, _frame: object) -> Never:
    """Convert either signal into the shared cancellation exception."""
    message = "renewal runner interrupted"
    raise RenewalInterruptError(message)


@contextlib.contextmanager
def interrupt_handlers() -> Iterator[None]:
    """Turn both signals into one cancellation, never a nested unwind."""
    cancelled = False

    def cancel(signum: int, frame: object) -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            interrupted(signum, frame)

    with ExitStack() as installed:
        with blocked_launch_signals():
            for signum in LAUNCH_SIGNALS:
                previous = signal.signal(signum, cancel)
                installed.enter_context(
                    always_cleanup(
                        partial(signal.signal, signum, previous),
                        f"{signum.name} handler restore failed",
                    )
                )
        yield


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
        WSGI_APPLICATION,
    ]


def _require_subreaper() -> None:
    # Orphaned Gunicorn workers must be reparented here, not to PID 1.
    if ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        _fail("could not adopt orphaned Gunicorn workers")


def _wait_for_eof(read_fd: int, deadline: float) -> bool:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([read_fd], [], [], remaining)[0]:
            return False
        if not os.read(read_fd, 4096):
            return True


def _reap_adopted_workers(pgid: int, deadline: float) -> None:
    # EOF can precede a child becoming waitable by a scheduling tick.
    # Drain only this group's adopted children, never unrelated subprocesses.
    while time.monotonic() < deadline:
        try:
            os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
    _fail("Gunicorn process group still exists after teardown deadline")


def _signal_group_and_wait(pgid: int, read_fd: int, signum: signal.Signals) -> bool:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass
    except OSError as error:
        message = f"could not send {signum.name} to Gunicorn process group"
        raise SupervisorError(message) from error
    try:
        return _wait_for_eof(read_fd, time.monotonic() + TERMINATION_GRACE_SECONDS)
    except (OSError, ValueError) as error:
        message = "could not observe Gunicorn liveness pipe EOF"
        raise SupervisorError(message) from error


def _reap_group(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        message = "Gunicorn master did not exit before teardown deadline"
        raise SupervisorError(message) from error
    except OSError as error:
        message = "could not reap Gunicorn master"
        raise SupervisorError(message) from error
    try:
        _reap_adopted_workers(process.pid, deadline)
    except OSError as error:
        message = "could not reap adopted Gunicorn workers"
        raise SupervisorError(message) from error


def _kill_and_reap(
    process: subprocess.Popen[bytes], read_fd: int, primary: BaseException | None = None
) -> None:
    # A failed observation must not bypass the KILL or reap steps.
    with (
        always_cleanup(lambda: _reap_group(process), "Gunicorn reap failed", primary),
        preserve_cleanup_error(primary, "Gunicorn KILL/EOF failed"),
    ):
        if not _signal_group_and_wait(process.pid, read_fd, signal.SIGKILL):
            _fail("Gunicorn liveness pipe did not reach EOF before teardown deadline")


def _terminate_group(process: subprocess.Popen[bytes], read_fd: int) -> None:
    """Signal the owned group and wait for every inherited writer to close."""
    try:
        eof = _signal_group_and_wait(process.pid, read_fd, signal.SIGTERM)
    except BaseException as error:
        _kill_and_reap(process, read_fd, error)
        raise
    if eof:
        _reap_group(process)
    else:
        _kill_and_reap(process, read_fd)


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
    fd = int(library.timerfd_create(time.CLOCK_MONOTONIC, 0))
    if fd < 0:
        _fail("could not open a Gunicorn port release timer")
    with always_cleanup(lambda: os.close(fd), "port release timer close failed"):
        inherit_across_exec = False
        os.set_inheritable(fd, inherit_across_exec)
        seconds = int(until)
        expiration = _Timespec(seconds, int((until - seconds) * 1e9))
        timer = _TimerSpec(_Timespec(), expiration)
        armed = library.timerfd_settime(
            fd, TFD_TIMER_ABSTIME, ctypes.byref(timer), None
        )
        if armed != 0:
            _fail("could not arm the Gunicorn port release timer")
        os.read(fd, 8)


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


def _require_complete_proof(port: int) -> None:
    try:
        require_released_port(port)
    except SupervisorError:
        raise
    except BaseException as error:
        message = "Gunicorn listener port was not released: proof did not complete"
        raise SupervisorError(message) from error


@contextlib.contextmanager
def prove_port_release(port: int | None) -> Iterator[None]:
    """Prove release even on failed teardown, preserving the primary error."""
    with always_cleanup(
        lambda: _require_complete_proof(port) if port is not None else None,
        "port release proof failed",
    ):
        yield


def teardown_master(master: SupervisedMaster, port: int | None) -> None:
    """Run the same bounded teardown and ordinary bind for every spawned server."""

    def prove() -> None:
        if port is not None:
            _require_complete_proof(port)
            master.release_checked = True

    # All harness helper threads inherit this mask. Restore it only after
    # reaping, closing, proving release, and recording the outcome.
    with blocked_launch_signals():
        if port is not None and getattr(master, "release_checked", False) is True:
            return
        with always_cleanup(prove, "port release proof failed"):
            try:
                master.terminate()
            except BaseException as error:
                with preserve_cleanup_error(error, "Gunicorn teardown retry failed"):
                    master.terminate()
                raise


class SupervisedMaster:
    """Own one Gunicorn master process group for the session lifetime."""

    def __init__(self, process: subprocess.Popen[bytes], read_fd: int) -> None:
        """Retain the forked master and its process group."""
        self.process = process
        # Popen completes setsid() before returning, even if exec exits fast.
        self.pgid = process.pid
        self._read_fd: int | None = read_fd
        self.release_checked = False
        self.log_stack = ExitStack()
        self.log_streams: list[BinaryIO] = []

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
        read_fd = self._read_fd
        if read_fd is None:
            return
        with (
            blocked_launch_signals(),
            always_cleanup(self.log_stack.close, "server log publication failed"),
        ):
            try:
                _terminate_group(self.process, read_fd)
            except BaseException as error:
                # A partial teardown must retain the reader until its retry can
                # make an actual termination attempt.
                with preserve_cleanup_error(
                    error, "Gunicorn group teardown retry failed"
                ):
                    _terminate_group(self.process, read_fd)
                with preserve_cleanup_error(error, "liveness reader close failed"):
                    self._close_reader(read_fd)
                raise
            else:
                self._close_reader(read_fd)

    def _close_reader(self, read_fd: int) -> None:
        self._read_fd = None
        os.close(read_fd)

    def log_snapshot(self) -> tuple[str, ...]:
        """Observe private live logs without publishing or changing their offsets."""
        snapshots = []
        for stream in self.log_streams:
            fd = stream.fileno()
            size = os.fstat(fd).st_size
            # Reopen the anonymous inode to obtain an independent read offset.
            with preserving_context(
                Path(f"/proc/self/fd/{fd}").open("rb"), "live log reader close failed"
            ) as reader:
                snapshots.append(reader.read(size).decode("utf-8", errors="replace"))
        return tuple(snapshots)


def start_signal_blocked_thread(thread: Thread) -> None:
    """Start a helper with a mask that keeps cancellation on the owner."""
    with blocked_launch_signals():
        thread.start()


@contextlib.contextmanager
def blocked_launch_signals() -> Iterator[set[int | signal.Signals]]:
    """Defer either launcher signal until the entire guarded operation exits."""
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, LAUNCH_SIGNALS)
    with always_cleanup(
        lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask),
        "signal mask restore failed",
    ):
        yield previous_mask


@contextlib.contextmanager
def owned_child[T](
    spawn: Callable[[set[int | signal.Signals]], T],
    stop: Callable[[T], object],
) -> Iterator[T]:
    """Publish a child handle before unmasking and protect its finalizer."""
    child: T | None = None

    def cleanup() -> None:
        if child is not None:
            with blocked_launch_signals():
                stop(child)

    with always_cleanup(cleanup, "owned child cleanup failed"):
        with blocked_launch_signals() as mask:
            child = spawn(mask)
        yield child


def child_argv(argv: list[str], mask: set[int | signal.Signals], /) -> list[str]:
    """Restore the child's mask post-exec, avoiding preexec_fn with threads."""
    return [
        sys.executable,
        "-c",
        "import os, signal, sys; "
        "signal.pthread_sigmask(signal.SIG_SETMASK, map(int, sys.argv[1].split(',')) "
        "if sys.argv[1] else ()); os.execvp(sys.argv[2], sys.argv[2:])",
        ",".join(map(str, sorted(mask))),
        *argv,
    ]


def _capture_server_logs(
    sink: DiagnosticSink, stack: ExitStack, log: Path, argv: list[str]
) -> tuple[list[BinaryIO], list[str], tuple[int, ...]]:
    streams = [stack.enter_context(sink.capture(log, server_log=True))]
    if "--access-logfile" not in argv:
        return streams, argv, ()
    index = argv.index("--access-logfile") + 1
    access = stack.enter_context(sink.capture(Path(argv[index])))
    streams.append(access)
    fd = access.fileno()
    return streams, [*argv[:index], f"/proc/self/fd/{fd}", *argv[index + 1 :]], (fd,)


def start_master(
    argv: list[str],
    environment: dict[str, str],
    log: Path,
    *,
    owner: list[SupervisedMaster],
    pass_fds: tuple[int | socket.socket, ...] = (),
) -> SupervisedMaster:
    """Perform the journaled second phase: exec the master in a new group.

    SIGINT/SIGTERM are blocked for the whole spawn→publish interval, so a
    cancellation can never land while a live spawned process is unowned:
    the child restores the inherited mask before exec, the handle is
    published into ``owner``, and only then is the mask restored — a
    pending cancellation lands inside the protected region, where the
    group is reaped before the exception propagates. Any other exception
    between the spawn and the completed handoff reaps the same way.
    A socket in ``pass_fds`` is the reserved listener, closed and checked
    by rollback before propagating the launch error.
    """
    _require_subreaper()
    listener = next((fd for fd in pass_fds if isinstance(fd, socket.socket)), None)
    inherited = tuple(fd if isinstance(fd, int) else fd.fileno() for fd in pass_fds)
    process: subprocess.Popen[bytes] | None = None
    master: SupervisedMaster | None = None
    port = int(listener.getsockname()[1]) if listener is not None else None
    sink = diagnostics(environment)
    log_stack = ExitStack()
    read_fd: int | None = None

    def close_unowned_reader() -> None:
        if read_fd is not None:
            os.close(read_fd)

    def close_unowned_logs() -> None:
        if master is None:
            log_stack.close()

    with (
        always_cleanup(close_unowned_reader, "liveness reader close failed"),
        always_cleanup(close_unowned_logs, "unowned server log publication failed"),
    ):
        try:
            with blocked_launch_signals() as previous_mask:
                read_fd, write_fd = os.pipe()
                with always_cleanup(
                    lambda: os.close(write_fd), "liveness writer close failed"
                ):
                    log_streams, argv, log_fds = _capture_server_logs(
                        sink, log_stack, log, argv
                    )
                    process = subprocess.Popen(  # noqa: S603 - frozen argv, resolved interpreter.
                        argv,
                        env=environment,
                        pass_fds=(*inherited, *log_fds, write_fd),
                        start_new_session=True,
                        # The child must not inherit the blocked mask.
                        preexec_fn=lambda: signal.pthread_sigmask(  # noqa: PLW1509 - single-threaded spawn; the hook is one sigmask call.
                            signal.SIG_SETMASK,
                            previous_mask,
                        ),
                        stdout=log_streams[0],
                        stderr=subprocess.STDOUT,
                    )
                    master = SupervisedMaster(process, read_fd)
                    master.log_stack = log_stack
                    master.log_streams = log_streams
                    read_fd = None
                owner.append(master)
            return master  # noqa: TRY300 - a deferred signal can land at the return.
        except BaseException as error:
            with (
                preserve_cleanup_error(error, "failed-launch group teardown failed"),
                blocked_launch_signals(),
            ):
                if master is None and process is not None and read_fd is not None:
                    master = SupervisedMaster(process, read_fd)
                    master.log_stack = log_stack
                    read_fd = None
                if master is not None:
                    _rollback_master(master, listener, port)
            raise


def _rollback_master(
    master: SupervisedMaster, listener: socket.socket | None, port: int | None
) -> None:
    with always_cleanup(
        lambda: teardown_master(master, port), "launch rollback failed"
    ):
        if listener is not None:
            listener.close()


CONTROLLER_MODULE: Final = "ops.testing.browser_server_controller"


def main() -> int:
    """Keep the controller in the launcher PID so signals reach its owner."""
    interpreter = sys.executable
    os.execv(  # noqa: S606 - fixed interpreter and closed module argv, never a shell.
        interpreter, [interpreter, "-m", CONTROLLER_MODULE, *sys.argv[1:]]
    )


if __name__ == "__main__":
    sys.exit(main())
