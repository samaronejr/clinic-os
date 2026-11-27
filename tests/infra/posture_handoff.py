"""Real two-consumer posture handoff. Only allowlisted metadata leaves the probe."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest

from infra.posture_child_lifetime import owned_spawn, waitable_children, worker_output
from infra.posture_observation import checkpoint
from infra.test_db_posture_url import MAKE_TIMEOUT_SECONDS
from isolated_pg_fixtures import migrated_lease, set_cluster_environment

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ops.testing.renewal_runner import ProvisionedDatabase


def _identity(db: psycopg.Connection) -> int:
    row = db.execute(
        "SELECT system_identifier FROM pg_catalog.pg_control_system()"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _identity_after(lease: ProvisionedDatabase) -> int:
    with psycopg.connect(lease.super_dsn, autocommit=True, connect_timeout=2) as db:
        return _identity(db)


def _run(lease: ProvisionedDatabase, *, blocked: bool = False) -> dict[str, object]:
    with pytest.MonkeyPatch.context() as mp:
        set_cluster_environment(lease, mp)
        mp.setenv("CLINIC_POSTURE_TIMING", "1")
        mp.setenv(
            "PYTHONPATH",
            os.pathsep.join(
                (
                    str(Path(__file__).resolve().parents[1]),
                    str(Path(__file__).resolve().parents[2]),
                )
            ),
        )
        if blocked:
            # Fault control only. The fresh and warm probes use the real uv.
            mp.setenv(
                "UV",
                f"{sys.executable} -I "
                f"{Path(__file__).with_name('posture_blocked_uv.py')}",
            )
        # The outer worker also has a handle-bound private kill domain. Never
        # use subprocess.run(timeout=...), whose error path signals by PID.
        until = time.monotonic() + MAKE_TIMEOUT_SECONDS + 16
        with (
            waitable_children() as inherited,
            owned_spawn("worker", until) as (proc, identity),
        ):
            stdout = worker_output(proc, identity, until)
    value = json.loads(stdout)
    assert isinstance(value, dict)
    value["launcher_wait"] = {"inherited": inherited, "restored": True}
    return value


def _assert_trace(data: dict[str, object], *, blocked: bool = False) -> None:
    # The message is safe even when the timing condition fails: the worker
    # never serializes argv, process IDs, database names, SQL, or error text.
    detail = json.dumps(data, separators=(",", ":"))
    assert data["timed_out"] is blocked, detail
    assert data["group_reaped"] is True, detail
    assert data["limit_seconds"] == MAKE_TIMEOUT_SECONDS
    assert data["after"] == data["before"]
    samples = data["samples"]
    stages = data["stages"]
    assert isinstance(stages, list)
    assert isinstance(samples, list)
    assert samples
    assert all("observer_error" not in sample for sample in samples), detail
    labels = [s["stage"] for s in stages]
    if blocked:
        assert labels == ["make-launch", "uv-launch"], detail
        assert data["processes_at_deadline"], detail
        assert isinstance(data["last_sample_before_deadline"], dict), detail
        assert data["exit"] != 0
    else:
        assert data["exit"] == 0, detail
        assert int(str(data["elapsed_ns"])) < MAKE_TIMEOUT_SECONDS * 1_000_000_000, (
            detail
        )
        assert labels[:5] == [
            "make-launch",
            "uv-launch",
            "verifier-start",
            "primary-connect",
            "primary-query",
        ], detail
        assert labels[5] == "primary-closed", detail
        assert (
            labels[6:]
            == [
                "peer-connect",
                "peer-query",
                "role-query",
                "routine-query",
                "peer-finished",
            ]
            * 5
        ), detail
        assert [s["ordinal"] for s in stages if s["stage"] == "peer-finished"] == [
            1,
            2,
            3,
            4,
            5,
        ]
        assert not any(
            any(
                row["role"] == "app" and row["state"] == "idle in transaction"
                for row in snap["backends"]
            )
            for snap in samples
            if snap["ns"] >= stages[5]["ns"]
        ), detail


@contextmanager
def handoff(
    lease: ProvisionedDatabase,
    root: Path,
    save: Callable[[dict[str, object]], None],
) -> Iterator[None]:
    trace: dict[str, object] = {}
    with psycopg.connect(lease.super_dsn, autocommit=True, connect_timeout=2) as db:
        identity = _identity(db)
        initial = checkpoint(db, "before-prior-consumer")
        assert initial["backends"] == []
    yield  # Real populated migration, graph-leaf reinstallation and clone teardown.
    with psycopg.connect(lease.super_dsn, autocommit=True, connect_timeout=2) as db:
        after = checkpoint(db, "after-prior-teardown")
        assert _identity(db) == identity
        assert after["backends"] == []
    idle_start = time.monotonic_ns()
    # Independent useful work is the idle barrier: the warm lease has no
    # open monitor, pooled connection, or synthetic sleep during this interval.
    with migrated_lease(root) as fresh:
        assert _identity_after(fresh) != identity
        trace["fresh"] = _run(fresh)
    idle_end = time.monotonic_ns()
    with psycopg.connect(lease.super_dsn, autocommit=True, connect_timeout=2) as db:
        before = checkpoint(db, "before-second-consumer")
        assert _identity(db) == identity
        assert before["backends"] == []
    trace.update(
        checkpoints=[initial, after, before],
        idle_ns=idle_end - idle_start,
        same_lease=True,
        distinct_fresh_control=True,
    )
    warm = _run(lease)
    trace["warm"] = warm
    save(trace)
    assert isinstance(trace["fresh"], dict)
    _assert_trace(trace["fresh"])
    _assert_trace(warm)
    fault = _run(lease, blocked=True)
    trace["injected_launcher_wait"] = fault
    save(trace)
    _assert_trace(fault, blocked=True)
