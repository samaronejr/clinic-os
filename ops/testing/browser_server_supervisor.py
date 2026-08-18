"""Durable supervisor that owns the Gunicorn master for the browser session.

The thin shell execs this module. The supervisor never runs product code: it
serves one authenticated request protocol for its journal-bound controller
child, performs the journaled two-phase fork/exec of the Gunicorn master into
its own process group, and guarantees a bounded TERM/wait/KILL/reap teardown so
no master, worker, or listener can outlive the session.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
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
MAX_MESSAGE_BYTES: Final = 4096
TERMINATION_GRACE_SECONDS: Final = 10
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


def supervised_argv(interpreter: Path, port: int, access_log: Path) -> list[str]:
    """Return the frozen two-worker preloaded Gunicorn argv."""
    if not interpreter.is_absolute() or not os.access(interpreter, os.X_OK):
        _fail("supervised interpreter is not an executable absolute path")
    return [
        str(interpreter),
        "-m",
        "gunicorn",
        "--bind",
        f"127.0.0.1:{port}",
        f"--workers={WORKER_COUNT}",
        "--preload",
        "--access-logfile",
        str(access_log),
        "--error-logfile",
        "-",
        "config.wsgi:application",
    ]


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
        if self.process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGTERM)
        try:
            self.process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pgid, signal.SIGKILL)
            self.process.wait(timeout=TERMINATION_GRACE_SECONDS)


def start_master(
    argv: list[str], environment: dict[str, str], log: Path
) -> SupervisedMaster:
    """Perform the journaled second phase: exec the master in a new group."""
    with log.open("wb") as stream:
        process = subprocess.Popen(  # noqa: S603 - frozen argv, resolved interpreter.
            argv,
            env=environment,
            start_new_session=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    return SupervisedMaster(process)


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
