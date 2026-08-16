"""Authenticate prior-boot process disappearance without signaling a PID."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

if TYPE_CHECKING:
    from pathlib import Path

MAX_PROC_STAT_BYTES: Final = 64 * 1024
START_TICKS_INDEX: Final = 19


def require_prior_processes_absent(identity: JsonObject, proc_root: Path) -> None:
    """Reject only a live PID whose procfs start identity is unchanged."""
    pairs = {
        (_integer(item.get("pid"), "process PID"), _start_ticks(item))
        for field in ("observed_members", "observed_listeners")
        for item in _objects(identity.get(field, []), field)
    }
    for pid, start_ticks in pairs:
        current = _current_start_ticks(proc_root, pid)
        if current == start_ticks:
            _fail("prior-boot process identity remains live")


def _current_start_ticks(proc_root: Path, pid: int) -> int | None:
    try:
        descriptor = os.open(
            proc_root / str(pid) / "stat",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return None
    try:
        raw = os.read(descriptor, MAX_PROC_STAT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_PROC_STAT_BYTES:
        _fail("process stat exceeds the bounded reader")
    marker = raw.rfind(b")")
    if not raw.startswith(f"{pid} (".encode()) or marker < 0:
        _fail("process stat identity is malformed")
    fields = raw[marker + 1 :].split()
    try:
        return int(fields[START_TICKS_INDEX])
    except (IndexError, ValueError) as error:
        message = "process stat identity is incomplete"
        raise IsolationError(message) from error


def _start_ticks(value: JsonObject) -> int:
    raw = value.get("start_ticks", value.get("process_start_ticks"))
    return _integer(raw, "process start ticks")


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} is invalid")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
