"""Dedicated subreaper for one timed Make/uv/verifier probe; stdout is safe JSON."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import IO

import psycopg
from ops.testing.final_e2e_supervisor import require_subreaper

from infra.posture_observation import activity, process_group
from infra.test_db_posture_url import MAKE_BINARY, MAKE_TIMEOUT_SECONDS

_STAGES = re.compile(
    r"posture-stage (verifier-start|primary-connect|primary-query|primary-closed|"
    r"peer-connect|peer-query|role-query|routine-query|peer-finished) (\d+) (\d+)"
)


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
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    # This process (not pytest) owns the subreaper; only reap this child's group.
    with suppress(ChildProcessError):
        while True:
            os.waitpid(-proc.pid, 0)
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return
    message = "posture child group survived teardown"
    raise RuntimeError(message)


def _probe(db: psycopg.Connection, deadline: float) -> dict[str, object]:
    assert MAKE_BINARY is not None
    assert Path(MAKE_BINARY).samefile("/usr/bin/make")
    marks: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    before = activity(db)
    stop = Event()
    timed_out = False
    at_deadline: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
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
            assert proc.stderr is not None
            reader = pool.submit(_stages, proc.stderr, marks)
            observer = pool.submit(_sample, db, proc.pid, stop, samples)
            try:
                proc.wait(
                    timeout=max(0, deadline - (time.monotonic_ns() - start) / 1e9)
                )
                end = time.monotonic_ns()
                timed_out = (end - start) / 1e9 >= deadline
            except subprocess.TimeoutExpired:
                end = time.monotonic_ns()
                timed_out = True
            finally:
                stop.set()
                try:
                    if timed_out:
                        at_deadline = process_group(proc.pid)
                finally:
                    # Sampling runs independently. Never delay termination to
                    # wait for another DB response on a stalled cluster.
                    _reap_group(proc)
            reader.result()
            observer.result()
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
