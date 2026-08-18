"""Supervise one hosted claimed PostgreSQL daemon across workflow commands."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import JsonObject, JsonValue, canonical_bytes

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
STATE_NAME: Final = "clinic-phase1a-ci-postgres"
READY_BOUND_SECONDS: Final = 660
CLEANUP_BOUND_SECONDS: Final = 150
PRIVATE_FILE_MODE: Final = 0o600


def main() -> int:
    """Dispatch the exact up or down lifecycle form."""
    if sys.argv[1:] == ["up"]:
        _up()
        return 0
    if sys.argv[1:] == ["down"]:
        _down()
        return 0
    _fail("invalid controller invocation")
    return 2


def _root() -> Path:
    runner = os.environ.get("RUNNER_TEMP", "")
    path = Path(runner)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        _fail("RUNNER_TEMP is not an authenticated directory")
    return path / STATE_NAME


def _up() -> None:
    root = _root()
    root.mkdir(mode=0o700)
    _fsync(root.parent)
    log_fd = os.open(root / "daemon.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    argv = (
        sys.executable,
        "-m",
        "ops.testing.ci_postgres_daemon",
        str(root),
    )
    environment = {
        "HOME": os.environ["HOME"],
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONTZPATH": "",
        "UV_PROJECT_ENVIRONMENT": os.environ["UV_PROJECT_ENVIRONMENT"],
    }
    previous_directory = Path.cwd()
    try:
        os.chdir(PROJECT_ROOT)
        pid = os.posix_spawn(
            sys.executable,
            argv,
            environment,
            file_actions=(
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_CLOSE, log_fd),
            ),
            setsid=True,
        )
    finally:
        os.chdir(previous_directory)
    os.close(log_fd)
    deadline = time.monotonic() + READY_BOUND_SECONDS
    while time.monotonic() < deadline:
        if (root / "ready").is_file():
            record = _load_record(root / "supervisor.json")
            if record.get("pid") != pid or record.get("start_ticks") != _start(pid):
                _abort_up(root, pid)
                _fail("PostgreSQL daemon identity drifted")
            return
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            _cleanup_failed_up(root)
            _fail(f"PostgreSQL daemon exited before readiness: {status}")
        time.sleep(0.1)
    os.killpg(pid, signal.SIGTERM)
    _wait_for_exit(pid)
    _cleanup_failed_up(root)
    _fail("PostgreSQL daemon readiness timed out")


def _down() -> None:
    root = _root()
    if not root.exists():
        return
    record = _load_record(root / "supervisor.json")
    pid = _positive(record.get("pid"), "daemon PID")
    pgid = _positive(record.get("pgid"), "daemon process group")
    start = _positive(record.get("start_ticks"), "daemon start ticks")
    if pgid != pid or _start(pid) != start:
        _fail("PostgreSQL daemon cannot be authenticated")
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + CLEANUP_BOUND_SECONDS
    while time.monotonic() < deadline and _present(pid, start):
        time.sleep(0.1)
    if _present(pid, start):
        os.killpg(pgid, signal.SIGKILL)
        _fail("PostgreSQL daemon cleanup exceeded 150 seconds")
    if not (root / "cleaned").is_file():
        _fail("PostgreSQL daemon did not prove cleanup")
    _remove_root(root)


def _abort_up(root: Path, pid: int) -> None:
    if _start(pid) >= 0:
        os.killpg(pid, signal.SIGTERM)
        _wait_for_exit(pid)
    _cleanup_failed_up(root)


def _wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + CLEANUP_BOUND_SECONDS
    while time.monotonic() < deadline:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return
        time.sleep(0.1)
    os.killpg(pid, signal.SIGKILL)
    os.waitpid(pid, 0)


def _cleanup_failed_up(root: Path) -> None:
    if (root / "supervisor.json").exists() and not (root / "cleaned").is_file():
        _fail("PostgreSQL daemon failed without proving cleanup")
    _remove_root(root)


def _remove_root(root: Path) -> None:
    for name in (
        "ready",
        "cleaned",
        "ci-postgres.env",
        "supervisor.json",
        "daemon.log",
    ):
        (root / name).unlink(missing_ok=True)
    root.rmdir()
    _fsync(root.parent)


def _load_record(path: Path) -> JsonObject:
    value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"pgid", "pid", "start_ticks"}:
        _fail("PostgreSQL supervisor record is invalid")
    if path.stat().st_mode & 0o777 != PRIVATE_FILE_MODE or path.is_symlink():
        _fail("PostgreSQL supervisor record mode is invalid")
    return value


def _start(pid: int) -> int:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return int(fields[21])
    except (FileNotFoundError, IndexError, ValueError):
        return -1


def _present(pid: int, start: int) -> bool:
    return _start(pid) == start


def _positive(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{context} is invalid")
    return value


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_record(path: Path, value: JsonObject, mode: int = 0o600) -> None:
    """Create, flush, and parent-flush one private daemon record."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, canonical_bytes(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync(path.parent)


def _fail(message: str) -> Never:
    raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
