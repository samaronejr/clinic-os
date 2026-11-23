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
import functools
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Callable

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
PROTECTED_DATABASE_PORT: Final = 5432
MAX_PORT: Final = 65535
STOP_SIGNALS: Final = frozenset({signal.SIGINT, signal.SIGTERM})
# Harness-only Gunicorn hooks: each worker dies with its master.
GUNICORN_CONFIG: Final = "python:ops.testing.browser_gunicorn_config"
PR_SET_PDEATHSIG: Final = 1


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
        "--config",
        GUNICORN_CONFIG,
        "--access-logfile",
        str(access_log),
        # ADR-014: method + status + duration only; no request line or peer.
        "--access-logformat",
        "%(m)s %(s)s %(D)s",
        "--error-logfile",
        "-",
        "config.wsgi:application",
    ]


@functools.cache
def _prctl() -> Callable[..., int]:
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, *[ctypes.c_ulong] * 4]
    prctl.restype = ctypes.c_int
    return prctl


def stop_mask_available() -> bool:
    """Report whether this build can block SIGINT/SIGTERM (``pthread_sigmask``)."""
    return hasattr(signal, "pthread_sigmask")


@functools.cache
def parent_death_available() -> bool:
    """Report, once, whether libc ``prctl`` can tie a child to its parent.

    Independent of the stop mask: without ``prctl`` only parent-death is
    lost (the server then outlives a killed harness, as at a81da6e3); the
    masked spawn and teardown still work.
    """
    try:
        _prctl()
    except (AttributeError, OSError):
        sys.stderr.write(
            "browser supervisor: prctl is unavailable; the server is not "
            "killed with its harness on this build\n"
        )
        return False
    return True


def die_with_parent(parent_pid: int) -> None:
    """Ask the kernel to SIGKILL this process when its forking thread ends.

    Linux delivers the signal when the thread that forked this process
    exits, for any reason, SIGKILL included; fork from the main thread.
    """
    if _prctl()(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent_pid:
        # The parent ended before the request took effect.
        os._exit(1)


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Bounded TERM/wait/KILL/reap for one spawned process group."""
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=TERMINATION_GRACE_SECONDS)


class SupervisedMaster:
    """Own one Gunicorn master process group for the session lifetime."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        """Retain the forked master and its process group."""
        self.process = process
        self.pgid = os.getpgid(process.pid)

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
        _terminate_group(self.process)


def start_master(
    argv: list[str],
    environment: dict[str, str],
    log: Path,
    *,
    owner: list[SupervisedMaster],
    pass_fds: tuple[int, ...] = (),
) -> SupervisedMaster:
    """Perform the journaled second phase: exec the master in a new group.

    The master's lifetime follows the calling thread: the child sets
    PR_SET_PDEATHSIG before exec, so however this process ends (an
    interrupted teardown or SIGKILL included) the kernel kills the master,
    and each worker dies with the master through ``GUNICORN_CONFIG``. Call
    this from the main thread.

    Only SIGINT/SIGTERM are blocked, and only while the child is started and
    its handle stored in ``owner``: opening the log and any reap happen
    outside, so a caller's other signals (a deadline) interrupt them as
    before. A stop deferred by the mask lands at the restore, after the
    handle is stored, and the group is reaped before it propagates. The
    mask is per thread: start no helper thread first, since a signal taken
    by another thread still runs its handler on this one. Without the
    stop mask (``stop_mask_available``) this spawns as the baseline did;
    without ``prctl`` only parent-death is skipped.
    """
    started: list[subprocess.Popen[bytes]] = []
    with log.open("wb") as stream:

        def start(prepare: Callable[[], None] | None) -> subprocess.Popen[bytes]:
            return subprocess.Popen(  # noqa: S603 - frozen argv, resolved interpreter.
                argv,
                env=environment,
                pass_fds=pass_fds,
                start_new_session=True,
                preexec_fn=prepare,  # noqa: PLW1509 - single-threaded spawn; one sigmask and one prctl call.
                stdout=stream,
                stderr=subprocess.STDOUT,
            )

        try:
            if stop_mask_available():
                _spawn_and_record(start, started, owner)
            else:
                started.append(start(None))
                owner.append(SupervisedMaster(started[0]))
        except BaseException:
            # The mask is restored; reap whatever was spawned, retried once
            # so a second signal during cleanup cannot orphan the child.
            for process in started:
                for _attempt in range(2):
                    with contextlib.suppress(Exception):
                        _terminate_group(process)
                        break
            raise
    return owner[-1]


def _spawn_and_record(
    start: Callable[[Callable[[], None]], subprocess.Popen[bytes]],
    started: list[subprocess.Popen[bytes]],
    owner: list[SupervisedMaster],
) -> None:
    """Start the master and store its handle with only SIGINT/SIGTERM blocked."""
    parent_pid = os.getpid()
    follow_parent = parent_death_available()
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())

    def prepare_child() -> None:
        # Restore the inherited mask so the master stays killable.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if follow_parent:
            die_with_parent(parent_pid)

    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        started.append(start(prepare_child))
        owner.append(SupervisedMaster(started[0]))
    finally:
        # Nothing else here. If a caller handler pre-empts the first restore
        # the second still runs; a stop deferred by the mask raises after it.
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def end_recorded(masters: list[SupervisedMaster], caller_mask: set[int] | None) -> None:
    """End every recorded master, then restore ``caller_mask``.

    ``caller_mask`` is the caller's mask read before the run (``None``
    without a stop mask). Every signal is blocked first; a handler already
    pending raises at that call, after the block, and is re-raised once the
    masters are gone. Every signal stays blocked until the final restore,
    so no handler can run before it, and whatever arrived during teardown
    is delivered right after it. Callers run this twice (nested
    ``finally``) so a signal that cuts the first call short at its start
    is still followed by a complete teardown; ending an ended master is a
    no-op.
    """
    if caller_mask is None:
        for master in masters:
            master.terminate()
        return
    every = signal.valid_signals() if hasattr(signal, "valid_signals") else STOP_SIGNALS
    interrupted: BaseException | None = None
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, every)
    except BaseException as error:  # noqa: BLE001 - re-raised below
        interrupted = error
    try:
        # A second distinct handler pending at once raises here.
        signal.pthread_sigmask(signal.SIG_BLOCK, every)
    except BaseException as error:  # noqa: BLE001 - re-raised below
        interrupted = interrupted or error
    try:
        for master in masters:
            master.terminate()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, caller_mask)
    if interrupted is not None:
        raise interrupted


CONTROLLER_MODULE: Final = "ops.testing.browser_server_controller"


def main() -> int:
    """Fork the journal-bound controller child and reap it on every exit."""
    guarded = parent_death_available()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    interpreter = sys.executable
    supervisor_pid = os.getpid()
    pid = os.fork()
    if pid == 0:
        if guarded:
            # The controller, and through it the server, ends with this process.
            die_with_parent(supervisor_pid)
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
