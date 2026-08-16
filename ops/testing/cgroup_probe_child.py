"""Run the disposable cgroup capability-probe child behind a parent barrier."""

from __future__ import annotations

import ctypes
import json
import os
import signal
from pathlib import Path
from typing import Final, Never

PR_SET_PDEATHSIG: Final = 1
START_TICKS_INDEX: Final = 19


def run_probe_child(
    readiness_fd: int,
    release_fd: int,
    acknowledgement_fd: int,
    expected_parent_pid: int,
    expected_cgroup_relative_path: str,
) -> Never:
    """Report identity, wait for migration authority, then attest membership."""
    try:
        _set_parent_death_signal()
        if os.getppid() != expected_parent_pid:
            os._exit(71)
        os.setsid()
        pid = os.getpid()
        start_ticks = process_start_ticks(pid)
        ready = {
            "expected_parent_pid": expected_parent_pid,
            "pgid": os.getpgrp(),
            "pid": pid,
            "start_ticks": start_ticks,
        }
        _write_all(readiness_fd, _canonical(ready))
        os.close(readiness_fd)
        if os.read(release_fd, 1) != b"1":
            os._exit(72)
        os.close(release_fd)
        if (
            os.getppid() != expected_parent_pid
            or process_start_ticks(pid) != start_ticks
            or _current_cgroup() != expected_cgroup_relative_path
        ):
            os._exit(73)
        _write_all(acknowledgement_fd, b"1")
        os.close(acknowledgement_fd)
        while True:
            signal.pause()
    except (OSError, RuntimeError, TypeError, ValueError):
        os._exit(74)


def process_start_ticks(pid: int) -> int:
    """Read the stable Linux process-start identity from procfs."""
    raw = (Path("/proc") / str(pid) / "stat").read_bytes()
    marker = raw.rfind(b")")
    if marker < 0:
        message = "invalid process stat"
        raise RuntimeError(message)
    fields = raw[marker + 1 :].split()
    if len(fields) <= START_TICKS_INDEX:
        message = "process stat has no start ticks"
        raise RuntimeError(message)
    return int(fields[START_TICKS_INDEX])


def _set_parent_death_signal() -> None:
    library = ctypes.CDLL(None, use_errno=True)
    prctl = library.prctl
    prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _current_cgroup() -> str:
    rows = Path("/proc/self/cgroup").read_text().splitlines()
    if len(rows) != 1 or not rows[0].startswith("0::/"):
        message = "probe child is not in unified cgroup v2"
        raise RuntimeError(message)
    return rows[0][3:]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        view = view[os.write(descriptor, view) :]
