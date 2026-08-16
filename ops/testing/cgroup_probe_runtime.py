"""Provide bounded Linux runtime operations for the cgroup probe owner."""

from __future__ import annotations

import json
import os
import select
import stat
import time
from pathlib import Path
from typing import Final, Never

from ops.testing.cgroup_probe_child import process_start_ticks
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

READ_TIMEOUT_SECONDS: Final = 5.0
WAIT_TIMEOUT_SECONDS: Final = 5.0
MAX_READINESS_BYTES: Final = 4096


def _validate_parent(path: Path, expected: JsonObject) -> None:
    value = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or value.st_dev != expected.get("device")
        or value.st_ino != expected.get("inode")
        or not os.access(path, os.W_OK | os.X_OK, effective_ids=True)
    ):
        _fail("delegated cgroup parent identity drifted")


def _read_child_identity(descriptor: int) -> JsonObject:
    ready, _, _ = select.select([descriptor], [], [], READ_TIMEOUT_SECONDS)
    if ready != [descriptor]:
        _fail("probe child readiness timed out")
    raw = os.read(descriptor, MAX_READINESS_BYTES + 1)
    if not raw.endswith(b"\n") or len(raw) > MAX_READINESS_BYTES:
        _fail("probe child emitted invalid readiness bytes")
    try:
        value: JsonValue = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = "probe child readiness is not JSON"
        raise IsolationError(message) from error
    if not isinstance(value, dict) or set(value) != {
        "expected_parent_pid",
        "pgid",
        "pid",
        "start_ticks",
    }:
        _fail("probe child readiness has the wrong shape")
    return {
        "expected_parent_pid": _integer(value["expected_parent_pid"], "parent PID"),
        "probe_pgid": _integer(value["pgid"], "probe PGID"),
        "probe_pid": _integer(value["pid"], "probe PID"),
        "probe_start_ticks": _integer(value["start_ticks"], "start ticks"),
    }


def _validate_child_identity(value: JsonObject, pid: int, parent_pid: int) -> None:
    if (
        value["probe_pid"] != pid
        or value["probe_pgid"] != pid
        or value["expected_parent_pid"] != parent_pid
        or process_start_ticks(pid) != value["probe_start_ticks"]
        or _process_parent_pid(pid) != parent_pid
        or os.getpgid(pid) != pid
        or os.getsid(pid) != pid
    ):
        _fail("probe child identity does not match the direct child")


def _write_control(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _write_all(descriptor, raw)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        view = view[os.write(descriptor, view) :]


def _read_exact_byte(descriptor: int) -> bytes:
    ready, _, _ = select.select([descriptor], [], [], READ_TIMEOUT_SECONDS)
    return os.read(descriptor, 1) if ready == [descriptor] else b""


def _wait_child(pid: int) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return
        time.sleep(0.01)
    _fail("probe child did not terminate")


def _wait_unpopulated(child: Path) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        events = (child / "cgroup.events").read_text().splitlines()
        if "populated 0" in events:
            return
        time.sleep(0.01)
    _fail("probe child cgroup remained populated")


def _cgroup_members(child: Path) -> list[int]:
    return sorted(
        int(value)
        for value in (child / "cgroup.procs").read_text().splitlines()
        if value
    )


def _process_parent_pid(pid: int) -> int:
    raw = (Path("/proc") / str(pid) / "stat").read_bytes()
    marker = raw.rfind(b")")
    return int(raw[marker + 1 :].split()[1])


def _process_cgroup(pid: int) -> str:
    rows = (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
    if len(rows) != 1 or not rows[0].startswith("0::/"):
        _fail("probe process left unified cgroup v2")
    return rows[0][3:]


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{context} must be an integer")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
