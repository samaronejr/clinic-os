"""Dedicated subreaper for one timed Make/uv/verifier probe; stdout is safe JSON."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Thread
from typing import IO, TYPE_CHECKING

import psycopg
from ops.testing.final_e2e_supervisor import require_subreaper

from infra.posture_child_lifetime import (
    exited,
    owned_spawn,
    waitable_children,
)
from infra.posture_observation import ProcessWindow, activity
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


def _stages(pipe: IO[bytes], records: list[dict[str, object]]) -> None:
    for line in pipe:
        value = line.decode("ascii", errors="replace").strip()
        if value == "posture-launch uv":
            records.append(
                {"stage": "uv-launch", "ns": time.monotonic_ns(), "ordinal": 0}
            )
        elif m := _STAGES.fullmatch(value):
            records.append({"stage": m[1], "ordinal": int(m[2]), "ns": int(m[3])})


def _stage_reader(pipe: IO[bytes], marks: list[dict[str, object]]) -> Future[None]:
    # The reader owns a separate fd until EOF; closing Popen's streams after
    # reaping cannot discard the verifier's final stage or race a recycled fd.
    source = os.fdopen(os.dup(pipe.fileno()), "rb")

    def read() -> None:
        with source:
            _stages(source, marks)

    try:
        return _watch(read)
    except BaseException:
        source.close()
        raise


def _sample(
    monitor: psycopg.Connection,
    window: ProcessWindow,
    stop: Event,
    records: list[dict[str, object]],
) -> None:
    # Sampling cadence only: no result is accepted by waiting for a timer.
    while not stop.is_set():
        sample: dict[str, object] = {
            "ns": time.monotonic_ns(),
            "processes": window.snapshot(),
        }
        try:
            sample["backends"] = activity(monitor)
        except psycopg.Error as exc:
            # Retain the timeline even if the observer itself was unavailable.
            sample.update(backends=[], observer_error=type(exc).__name__)
        records.append(sample)
        stop.wait(0.05)


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
    until = start / 1e9 + deadline
    with owned_spawn("make", until) as (proc, fd):
        window = ProcessWindow(fd)
        pipe = proc.stderr
        assert pipe is not None
        try:
            reader = _stage_reader(pipe, marks)
            observer = _watch(lambda: _sample(db, window, stop, samples))
            finished = exited(fd, until)
            end = time.monotonic_ns()
            timed_out = not finished or (end - start) / 1e9 >= deadline
        finally:
            stop.set()
            try:
                if timed_out:
                    at_deadline = window.snapshot()
            finally:
                window.close()  # No numeric /proc read can cross ownership release.
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
    with waitable_children() as inherited:
        require_subreaper()
        with psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"],
            autocommit=True,
            connect_timeout=2,
            options="-c search_path=pg_catalog -c statement_timeout=500",
        ) as monitor:
            result = _probe(monitor, MAKE_TIMEOUT_SECONDS)
    result["child_wait"] = {"inherited": inherited, "restored": True}
    json.dump(result, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
