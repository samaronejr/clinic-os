"""Remain the sole F3 subreaper, child barrier, and direct-wait owner."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.testing.f3_cli import supervisor_entry
from ops.testing.f3_kill_domain import PathIdentity, move_process
from ops.testing.final_e2e_controller import CHILD_STAGES

PR_SET_PDEATHSIG: Final = 1
PR_SET_CHILD_SUBREAPER: Final = 36
TERM_GRACE_SECONDS: Final = 30.0
MIN_PRIVATE_FD: Final = 3
PROC_START_TICKS_INDEX: Final = 21
_SUBREAPER_STATE: dict[str, bool] = {"active": False}


class F3SupervisorError(RuntimeError):
    """Reject unauthenticated process, argv, barrier, or wait state."""


def _fail(reason: str) -> Never:
    raise F3SupervisorError(reason)


class _Prctl(Protocol):
    def __call__(self, option: int, value: int) -> int: ...


@dataclass(frozen=True, slots=True)
class ChildIdentity:
    """Bind one direct child PID/PGID to its Linux process start ticks."""

    pid: int
    pgid: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class ChildResult:
    """Expose one directly reaped exit code or signal without raw output."""

    exit_code: int | None
    signal_number: int | None
    timed_out: bool


@dataclass(frozen=True, slots=True)
class _ChildLaunch:
    parent_pid: int
    argv: tuple[str, ...]
    environment: dict[str, str]
    ready_read: int
    ready_write: int
    release_read: int
    release_write: int


def require_subreaper(prctl: _Prctl | None = None) -> None:
    """Set PR_SET_CHILD_SUBREAPER before the supervisor may fork."""
    selected = _system_prctl if prctl is None else prctl
    if selected(PR_SET_CHILD_SUBREAPER, 1) != 0:
        _fail("F3 subreaper activation failed")
    _SUBREAPER_STATE["active"] = True


def controller_argv(python: Path, script: Path, control_fd: int) -> tuple[str, ...]:
    """Build the one absolute isolated controller invocation."""
    _launcher(python, script)
    if control_fd < MIN_PRIVATE_FD:
        _fail("controller control descriptor rejected")
    return (
        str(python),
        "-I",
        "-P",
        "-B",
        str(script),
        "--control-fd",
        str(control_fd),
    )


def stage_argv(python: Path, script: Path, name: str) -> tuple[str, ...]:
    """Build only one allowed stages 1-4 or 9-12 child invocation."""
    _launcher(python, script)
    if name not in CHILD_STAGES:
        _fail("F3 child stage rejected")
    return (str(python), "-I", "-P", "-B", str(script), "--stage", name)


def minimal_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the closed child environment plus explicit private bindings."""
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }
    if extra is not None:
        if any(
            not key.startswith("CLINIC_F3_")
            or not value
            or "\x00" in key
            or "\x00" in value
            for key, value in extra.items()
        ):
            _fail("F3 child environment rejected")
        environment.update(extra)
    return environment


def spawn_barrier_child(
    argv: tuple[str, ...],
    environment: dict[str, str],
    domain: PathIdentity,
    record: Callable[[ChildIdentity, bool], None],
) -> ChildIdentity:
    """Fork, bind, journal, and only then release one direct child."""
    if not _SUBREAPER_STATE["active"] or not argv or not Path(argv[0]).is_absolute():
        _fail("F3 child spawn rejected")
    parent_pid = os.getpid()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    launch = _ChildLaunch(
        parent_pid,
        argv,
        environment,
        ready_read,
        ready_write,
        release_read,
        release_write,
    )
    pid = os.fork()
    if pid == 0:
        try:
            _child_pre_exec(launch)
        except OSError:
            os._exit(126)
        os._exit(126)
    os.close(ready_write)
    os.close(release_read)
    successful = False
    try:
        if os.read(ready_read, 1) != b"R":
            _fail("F3 child readiness barrier failed")
        move_process(domain, pid)
        identity = ChildIdentity(pid, pid, _start_ticks(pid))
        _record_barrier(record, identity, released=False)
        os.write(release_write, b"G")
        _record_barrier(record, identity, released=True)
        successful = True
        return identity
    finally:
        os.close(ready_read)
        os.close(release_write)
        if not successful:
            _terminate_and_reap(pid, pid, TERM_GRACE_SECONDS)


def wait_child(identity: ChildIdentity, seconds: float) -> ChildResult:
    """Wait directly, then TERM/KILL and reap on the bounded timeout path."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        waited, status = os.waitpid(identity.pid, os.WNOHANG)
        if waited == identity.pid:
            return _status_result(status, timed_out=False)
        time.sleep(0.05)
    status = _terminate_and_reap(identity.pid, identity.pgid, TERM_GRACE_SECONDS)
    result = _status_result(status, timed_out=True)
    return ChildResult(result.exit_code, result.signal_number, timed_out=True)


def _child_pre_exec(launch: _ChildLaunch) -> Never:
    if (
        _system_prctl(PR_SET_PDEATHSIG, signal.SIGKILL) != 0
        or os.getppid() != launch.parent_pid
    ):
        os._exit(125)
    os.setsid()
    os.close(launch.ready_read)
    os.close(launch.release_write)
    os.write(launch.ready_write, b"R")
    os.close(launch.ready_write)
    if os.read(launch.release_read, 1) != b"G" or os.getppid() != launch.parent_pid:
        os._exit(125)
    os.close(launch.release_read)
    _execve(launch.argv, launch.environment)


def _terminate_and_reap(pid: int, pgid: int, seconds: float) -> int:
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.05)
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    return status


def _status_result(status: int, *, timed_out: bool) -> ChildResult:
    if os.WIFEXITED(status):
        return ChildResult(os.WEXITSTATUS(status), None, timed_out)
    if os.WIFSIGNALED(status):
        return ChildResult(None, os.WTERMSIG(status), timed_out)
    _fail("F3 child wait status rejected")


def _start_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
    if (
        len(fields) <= PROC_START_TICKS_INDEX
        or not fields[PROC_START_TICKS_INDEX].isdecimal()
    ):
        _fail("F3 child process identity rejected")
    return int(fields[PROC_START_TICKS_INDEX])


def _launcher(python: Path, script: Path) -> None:
    if (
        not python.is_absolute()
        or python.as_posix().endswith("/.venv/bin/python") is False
        or not script.is_absolute()
        or script.name != "final_e2e_controller.py"
    ):
        _fail("F3 launcher or controller path rejected")


def _record_barrier(
    record: Callable[[ChildIdentity, bool], None],
    identity: ChildIdentity,
    *,
    released: bool,
) -> None:
    record(identity, released)


def _execve(argv: tuple[str, ...], environment: dict[str, str]) -> Never:
    encoded_argv = tuple(os.fsencode(item) for item in argv)
    encoded_environment = tuple(
        os.fsencode(f"{key}={value}") for key, value in sorted(environment.items())
    )
    argv_array = (ctypes.c_char_p * (len(encoded_argv) + 1))(*encoded_argv, None)
    env_array = (ctypes.c_char_p * (len(encoded_environment) + 1))(
        *encoded_environment,
        None,
    )
    library = ctypes.CDLL(None, use_errno=True)
    library.execve(encoded_argv[0], argv_array, env_array)
    os._exit(126)


def _system_prctl(option: int, value: int) -> int:
    library = ctypes.CDLL(None, use_errno=True)
    return int(library.prctl(option, value, 0, 0, 0))


def main(argv: list[str] | None = None) -> int:
    """Validate the outer form and fail closed without a complete F3 driver."""
    values = sys.argv[1:] if argv is None else argv
    return supervisor_entry(values, require_subreaper)


if __name__ == "__main__":
    raise SystemExit(main())
