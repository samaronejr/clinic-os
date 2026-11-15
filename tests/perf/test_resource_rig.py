"""Resource topology and the separation of producers from real admission."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from apps.core import fairness
from ops.perf import ai_load, fairness_runs, rig
from ops.perf.children import ChildContext
from ops.perf.runtime import PerfEnvironmentError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.perf import scenarios

PREFIX = "cpiperf" + "a" * 32
ORG_ID = UUID("00000000-0000-0000-0000-000000000071")
SCOPE = ChildContext(
    "synthetic", "synthetic-file", "/synthetic", str(ORG_ID), str(ORG_ID), str(ORG_ID)
)


@pytest.fixture
def _inprocess_load_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep mocked lifecycle children free of unused named semaphores."""
    context = Mock(wraps=multiprocessing.get_context("spawn"))
    context.Event = Event
    context.Array = lambda _typecode, values: list(values)
    module = Mock(get_context=Mock(return_value=context))
    monkeypatch.setattr(ai_load, "multiprocessing", module)
    monkeypatch.setattr(fairness_runs, "multiprocessing", module)


def test_application_budget_is_fixed_and_producers_are_external() -> None:
    server_roles: tuple[rig.Role, rig.Role] = ("clinical", "worker")
    assert rig.BUDGETS["application"] == (2, 4 * 1024**3)
    assert rig.BUDGETS["clinical"] == (1, 3 * 1024**3)
    assert rig.BUDGETS["worker"] == (1, 1024**3)
    assert sum(rig.BUDGETS[role][0] for role in server_roles) == 2
    assert sum(rig.BUDGETS[role][1] for role in server_roles) == 4 * 1024**3
    assert rig._slice(PREFIX, "clinical") == f"{PREFIX}-clinical.slice"
    assert rig._slice(PREFIX, "worker") == f"{PREFIX}-worker.slice"
    assert rig._slice(PREFIX, "producers") == f"{PREFIX}producers.slice"
    assert ai_load.PRODUCERS == 2


@pytest.mark.parametrize("cpu_max", ["100000 100000", "200000 100000", "max 100000"])
def test_snapshot_verifies_actual_cpu_and_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cpu_max: str
) -> None:
    group = tmp_path / "clinical"
    group.mkdir()
    values = {
        "cpu.max": cpu_max,
        "memory.max": str(3 * 1024**3),
        "memory.swap.max": "0",
        "cpu.stat": "usage_usec 123456\nthrottled_usec 234\n",
        "cpu.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
        "memory.current": "123",
        "memory.events": "oom 0\noom_kill 0\n",
    }
    for key, value in values.items():
        (group / key).write_text(value)
    monkeypatch.setattr(rig, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(rig, "_path", lambda *_args: group)
    if cpu_max == "100000 100000":
        result = rig._snapshot(PREFIX, "clinical")
        assert result["cpu_usage_usec"] == 123456
        assert result["memory_bytes"] == 3 * 1024**3
    else:
        with pytest.raises(PerfEnvironmentError, match="budget does not match"):
            rig._snapshot(PREFIX, "clinical")


def test_workers_refuse_to_run_without_the_canonical_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(rig.PREFIX_ENV, raising=False)
    with pytest.raises(PerfEnvironmentError, match="canonical resource rig"):
        rig.attach("worker")


def test_non_delegated_return_target_is_rejected_before_relocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = rig.ResourceRig()
    start = Mock()
    create = Mock()
    monkeypatch.setattr(scope, "_start_anchor", start)
    monkeypatch.setattr(rig, "_transient", create)
    monkeypatch.setattr(
        rig, "_command", lambda *_args: "Delegate=no\nActiveState=active"
    )
    with pytest.raises(PerfEnvironmentError, match="active delegated launcher"):
        scope.__enter__()
    start.assert_not_called()
    create.assert_not_called()


def test_systemd_failure_preserves_actual_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            1,
            "busctl",
            stderr="Process migration not available on non-delegated units.",
        )

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(PerfEnvironmentError, match="Process migration not available"):
        rig._command("/usr/bin/busctl", "call")


def test_failed_restoration_closes_control_channel_and_joins_anchor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scope = rig.ResourceRig()
    scope.attached = True
    scope.anchor_ready = True
    scope.anchor_channel, peer = multiprocessing.get_context("spawn").Pipe()
    anchor = Mock(pid=71, exitcode=0)
    anchor.is_alive.return_value = False
    monkeypatch.setattr(scope, "anchor", anchor)
    monkeypatch.setenv(rig.PREFIX_ENV, PREFIX)

    def denied(*_args: str) -> str:
        message = "synthetic restoration denied"
        raise PerfEnvironmentError(message)

    monkeypatch.setattr(rig, "_bus", denied)
    with pytest.raises(PerfEnvironmentError, match="restoration denied"):
        scope.__exit__(None, None, None)
    assert scope.anchor_channel.closed
    with pytest.raises(EOFError):
        peer.recv_bytes()
    peer.close()
    anchor.join.assert_called_once_with(30)
    assert rig.PREFIX_ENV not in os.environ
    assert "synthetic restoration denied" in capsys.readouterr().err


def test_successful_restoration_signals_cleanup_before_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = rig.ResourceRig()
    scope.attached = True
    scope.anchor_ready = True
    scope.anchor_channel, peer = multiprocessing.get_context("spawn").Pipe()
    restore = Mock(return_value="")
    monkeypatch.setattr(rig, "_bus", restore)
    scope.__exit__(None, None, None)
    assert not scope.attached
    assert peer.recv_bytes() == b"restored"
    with pytest.raises(EOFError):
        peer.recv_bytes()
    peer.close()
    restore.assert_called_once()


@pytest.mark.parametrize("normal_exit", [True, False])
def test_launcher_anchor_cleans_only_abnormal_parent_loss(
    monkeypatch: pytest.MonkeyPatch, normal_exit: bool
) -> None:
    calls: list[tuple[str, ...]] = []

    def command(*arguments: str) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(rig, "_command", command)
    client, anchor = multiprocessing.get_context("spawn").Pipe()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(rig._hold_launcher, PREFIX, anchor)
        assert client.recv_bytes() == b"ready"
        if normal_exit:
            client.send_bytes(b"restored")
        client.close()
        future.result(timeout=5)
    assert calls == (
        []
        if normal_exit
        else [
            (
                "/usr/bin/systemctl",
                "stop",
                f"{PREFIX}.slice",
                f"{PREFIX}producers.slice",
            )
        ]
    )


def test_worker_enters_budget_before_real_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(ai_load, "attach", order.append)
    monkeypatch.setattr(ai_load, "child_setup", lambda _context: order.append("setup"))
    monkeypatch.setattr(ai_load, "runtime_role", nullcontext)

    def acquire(*, organization_id: UUID, queue: str) -> bool:
        assert organization_id == ORG_ID
        assert queue == "ai-batch"
        order.append("admission")
        return True

    monkeypatch.setattr(fairness, "fair_acquire", acquire)
    spawn = multiprocessing.get_context("spawn")
    client, server = spawn.Pipe()
    ready, failed = spawn.Event(), spawn.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ai_load._worker, SCOPE, server, ready, failed)
        assert ready.wait(5)
        client.send_bytes(b"")
        assert client.recv_bytes(1) == b"1"
        client.close()
        future.result(timeout=5)
    assert order == ["worker", "setup", "admission"]
    assert not failed.is_set()


def test_producer_only_transports_results_and_counts_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles: list[str] = []
    monkeypatch.setattr(ai_load, "attach", roles.append)

    def forbidden(**_kwargs: object) -> bool:
        pytest.fail("the external producer executed business admission")

    monkeypatch.setattr(fairness, "fair_acquire", forbidden)
    spawn = multiprocessing.get_context("spawn")
    client, server = spawn.Pipe()
    stop, warm, failed = spawn.Event(), spawn.Event(), spawn.Event()
    counts = spawn.Array("q", [0, 0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ai_load._producer, client, stop, warm, counts, failed)
        assert server.recv_bytes(0) == b""
        server.send_bytes(b"1")
        assert server.recv_bytes(0) == b""
        stop.set()
        server.send_bytes(b"0")
        future.result(timeout=5)
        server.close()
    assert roles == ["producers"]
    assert counts[:] == [1, 1]
    assert warm.is_set()
    assert not failed.is_set()


@pytest.mark.parametrize("kind", ["ai", "bulk"])
@pytest.mark.usefixtures("_inprocess_load_context")
def test_partial_start_joins_only_children_that_started(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    load = (
        ai_load.AdmissionLoad(SCOPE, SCOPE)
        if kind == "ai"
        else fairness_runs.BackgroundLoad(None, 0, SCOPE)
    )
    first = Mock(exitcode=0)
    first.is_alive.return_value = False
    second = Mock()
    second.start.side_effect = RuntimeError("synthetic spawn failed")
    monkeypatch.setattr(load, "children", [first, second])
    with pytest.raises(RuntimeError, match="synthetic spawn failed"):
        load.__enter__()
    first.join.assert_called_once()
    second.join.assert_not_called()
    assert load.stop.is_set()


@pytest.mark.parametrize("kind", ["ai", "bulk"])
@pytest.mark.usefixtures("_inprocess_load_context")
def test_interrupt_during_warmup_cleans_up_started_children(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    load = (
        ai_load.AdmissionLoad(SCOPE, SCOPE)
        if kind == "ai"
        else fairness_runs.BackgroundLoad(None, 0, SCOPE)
    )
    started = Mock(exitcode=0)
    started.is_alive.return_value = False
    interrupted = Mock()
    interrupted.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(load, "children", [started])
    monkeypatch.setattr(load, "warm", [interrupted])
    with pytest.raises(KeyboardInterrupt):
        load.__enter__()
    started.join.assert_called_once()
    assert load.stop.is_set()


@pytest.mark.parametrize("kind", ["ai", "bulk"])
@pytest.mark.usefixtures("_inprocess_load_context")
def test_nonzero_child_exit_invalidates_the_trial(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    load = (
        ai_load.AdmissionLoad(SCOPE, SCOPE)
        if kind == "ai"
        else fairness_runs.BackgroundLoad(None, 0, SCOPE)
    )
    crashed = Mock(exitcode=9)
    crashed.is_alive.return_value = False
    monkeypatch.setattr(load, "started", [crashed])
    with pytest.raises(RuntimeError, match="process failed"):
        load.__exit__(None, None, None)
    assert load.failed.is_set()


@pytest.mark.usefixtures("_inprocess_load_context")
def test_victim_response_timeout_invalidates_the_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = ai_load.AdmissionLoad(SCOPE, SCOPE)
    original = load.victim
    stalled = Mock()
    stalled.poll.return_value = False
    monkeypatch.setattr(load, "victim", stalled)
    try:
        with pytest.raises(RuntimeError, match="response timed out"):
            load.victim_admitted()
        stalled.poll.assert_called_once_with(ai_load.START_TIMEOUT)
    finally:
        original.close()
        load.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("admitted", "deferred", "expected"),
    [(0, 0, "fail"), (0, 5, "fail"), (5, 0, "fail"), (1, 1, "pass")],
)
def test_warmup_counts_cannot_replace_flood_during_saves(
    monkeypatch: pytest.MonkeyPatch, admitted: int, deferred: int, expected: str
) -> None:
    load = Mock(first=100, second=100)
    load.victim_admitted.return_value = True

    def alternate(
        _prepared: object,
        _trials: int,
        _make: object,
        during: Callable[[Mock], object],
    ) -> tuple[float, float, dict[str, object]]:
        return (
            100.0,
            100.0,
            {
                "during": during(load),
                "trials_ms": [
                    {
                        "work_completed_during_saves": admitted,
                        "work_deferred_during_saves": deferred,
                        "loaded_window_seconds": 10.0,
                    }
                ],
            },
        )

    monkeypatch.setattr(fairness_runs, "_context", lambda *_args, **_kwargs: SCOPE)
    monkeypatch.setattr(fairness_runs, "_alternate", alternate)
    monkeypatch.setattr(fairness_runs, "runtime_role", nullcontext)
    prepared = cast("scenarios.Prepared", Mock())
    result = fairness_runs.ai_batch_flood(prepared, trials=1)
    assert result["status"] == expected
    assert result["load"]["totals"]["offered"] == admitted + deferred
    assert (
        result["load"]["trials_ms"][0]["completed_response_rate_per_second"]
        == (admitted + deferred) / 10.0
    )


def test_owner_resource_decision_remains_explicit() -> None:
    text = (Path(__file__).resolve().parents[2] / "docs/operations/slo.md").read_text()
    for phrase in (
        "2026-11-13",
        "Clinic Ops owner",
        "1.124",
        "69.386",
        "77.993",
        "3 GiB",
        "1 GiB",
        "outside the application",
        "lower offered",
    ):
        assert phrase in text
