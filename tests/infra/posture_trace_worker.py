"""Dedicated subreaper for one timed Make/uv/verifier probe; stdout is safe JSON."""

from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Thread
from typing import IO, TYPE_CHECKING

import psycopg
from ops.testing.final_e2e_supervisor import require_subreaper

from infra.posture_observation import activity, process_group
from infra.test_db_posture_url import MAKE_BINARY, MAKE_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

_STAGES = re.compile(
    r"posture-stage (verifier-start|primary-connect|primary-query|primary-closed|"
    r"peer-connect|peer-query|role-query|routine-query|peer-finished) (\d+) (\d+)"
)
_CLEANUP_SECONDS = 3


def _watch(operation: Callable[[], None]) -> Future[None]:
    """A stuck observer must not hold the private worker open at interpreter exit."""
    result: Future[None] = Future()

    def run() -> None:
        try:
            operation()
        except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
            result.set_exception(RuntimeError(type(exc).__name__))
        else:
            result.set_result(None)

    Thread(target=run, daemon=True).start()
    return result


def _observe_exit(pid: int, until: float) -> bool:
    """Poll the kernel handle for exit without consuming the child wait status."""
    fd = os.pidfd_open(pid)
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(max(0, math.ceil((until - time.monotonic()) * 1000))))
    finally:
        os.close(fd)


def _stages(pipe: IO[bytes], records: list[dict[str, object]]) -> None:
    for line in pipe:
        value = line.decode("ascii", errors="replace").strip()
        if value == "posture-launch uv":
            records.append(
                {"stage": "uv-launch", "ns": time.monotonic_ns(), "ordinal": 0}
            )
        elif m := _STAGES.fullmatch(value):
            records.append({"stage": m[1], "ordinal": int(m[2]), "ns": int(m[3])})


def _sample(
    monitor: psycopg.Connection,
    pgid: int,
    stop: Event,
    records: list[dict[str, object]],
) -> None:
    # Sampling cadence only: no result is accepted by waiting for a timer.
    while not stop.is_set():
        sample: dict[str, object] = {
            "ns": time.monotonic_ns(),
            "processes": process_group(pgid),
        }
        try:
            sample["backends"] = activity(monitor)
        except psycopg.Error as exc:
            # Retain the timeline even if the observer itself was unavailable.
            sample.update(backends=[], observer_error=type(exc).__name__)
        records.append(sample)
        stop.wait(0.05)


def _reap_group(proc: subprocess.Popen[bytes]) -> None:
    # This dedicated worker is the sole waiter. A live or zombie direct child
    # pins the numeric PGID. Never signal even with signal 0 after releasing it.
    if proc.returncode is not None:
        return
    until = time.monotonic() + _CLEANUP_SECONDS
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=max(0, until - time.monotonic()))
    # Only this subprocess's children can be waited here. Every descendant
    # was in the killed private session; adopted zombies also have to be reaped.
    children = Path("/proc/thread-self/children")
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid:
                continue
            if time.monotonic() >= until:
                break
            adopted = children.read_text().split()
            if adopted and not _observe_exit(int(adopted[0]), until):
                break
    except ChildProcessError:
        return
    message = "posture child cleanup exceeded its separate deadline"
    raise TimeoutError(message)


def _probe(db: psycopg.Connection, deadline: float) -> dict[str, object]:
    assert MAKE_BINARY is not None
    assert Path(MAKE_BINARY).samefile("/usr/bin/make")
    marks: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    before = activity(db)
    stop = Event()
    timed_out = False
    at_deadline: list[dict[str, object]] = []
    start = time.monotonic_ns()
    marks.append({"stage": "make-launch", "ns": start, "ordinal": 0})
    with subprocess.Popen(
        ("/usr/bin/make", "db-posture", "DOCKER=true"),
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as proc:
        pipe = proc.stderr
        assert pipe is not None
        try:
            reader = _watch(lambda: _stages(pipe, marks))
            observer = _watch(lambda: _sample(db, proc.pid, stop, samples))
            exited = _observe_exit(proc.pid, start / 1e9 + deadline)
            end = time.monotonic_ns()
            timed_out = not exited or (end - start) / 1e9 >= deadline
        finally:
            stop.set()
            try:
                if timed_out:
                    at_deadline = process_group(proc.pid)
            finally:
                # Never wait on an observer while the owned group needs killing.
                _reap_group(proc)
        until = time.monotonic() + _CLEANUP_SECONDS
        for result in (reader, observer):
            result.result(timeout=max(0, until - time.monotonic()))
    return {
        "exit": proc.returncode,
        "timed_out": timed_out,
        "limit_seconds": deadline,
        "start_ns": start,
        "end_ns": end,
        "elapsed_ns": end - start,
        "stages": marks,
        "samples": samples,
        "processes_at_deadline": at_deadline,
        "last_sample_before_deadline": next(
            (row for row in reversed(samples) if int(str(row["ns"])) <= end), None
        ),
        "before": before,
        "after": activity(db),
        "group_reaped": True,
    }


def main() -> None:
    require_subreaper()
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"],
        autocommit=True,
        connect_timeout=2,
        options="-c search_path=pg_catalog -c statement_timeout=500",
    ) as monitor:
        result = _probe(monitor, MAKE_TIMEOUT_SECONDS)
    json.dump(result, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
