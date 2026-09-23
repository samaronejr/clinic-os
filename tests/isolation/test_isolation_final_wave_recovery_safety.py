from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing import isolation_common as c
from ops.testing import isolation_controller_kernel as kernel
from ops.testing import isolation_final_wave_controller as final_controller
from ops.testing.cgroup_probe_child import process_start_ticks

from isolation.isolation_final_wave_engine_fixtures import (
    BOOT,
    NOW,
    WORKSPACE_CLAIM,
    final_start,
)
from isolation.isolation_process_fixtures import blocking_python_argv, running_process

if TYPE_CHECKING:
    from pathlib import Path

SECOND_CLAIM: Final = "77777777-7777-4777-8777-777777777777"
ALIVE: Final = kernel.RecoveryPolicy(lambda _pid, _ticks: True)
DEAD: Final = kernel.RecoveryPolicy(lambda _pid, _ticks: False)


def test_final_wave_acquire_replays_the_exact_staging_claim(tmp_path: Path) -> None:
    # Given: a durable final-form acquisition bound to staging claim A.
    request = final_start(tmp_path, BOOT, 4100, "final")
    first = final_controller.acquire_final_wave(request, ALIVE)
    first.abandon()

    # When: the same invocation retries with a new proven-dead controller owner.
    replay = replace(request, owner_pid=4101, owner_start_ticks=8101)
    recovered = final_controller.acquire_final_wave(replay, DEAD)

    # Then: exact claim A remains bound across the idempotent retry.
    assert recovered.record["final_gate_staging_claim_id"] == WORKSPACE_CLAIM
    recovered.abandon()


def test_final_wave_acquire_rejects_staging_claim_drift(tmp_path: Path) -> None:
    # Given: a durable final-form acquisition bound to staging claim A.
    request = final_start(tmp_path, BOOT, 4200, "final")
    first = final_controller.acquire_final_wave(request, ALIVE)
    first.abandon()
    drifted = replace(
        request,
        owner_pid=4201,
        owner_start_ticks=8201,
        final_gate_staging_claim_id=SECOND_CLAIM,
    )

    # When: the retry substitutes staging claim B.
    try:
        recovered = final_controller.acquire_final_wave(drifted, DEAD)
    except c.IsolationError as error:
        rejection = str(error)
    else:
        recovered.abandon()
        rejection = ""

    # Then: acquisition identity rejects the otherwise valid claim UUID.
    assert "binding drifted" in rejection


def test_live_child_blocks_cleanup_verified_failure_recovery(tmp_path: Path) -> None:
    # Given: a dead controller owner left an inert, identity-bound child running.
    request = final_start(tmp_path, BOOT, 4300)
    session = final_controller.acquire_final_wave(request, ALIVE)
    checks: list[tuple[int, int]] = []
    with running_process(blocking_python_argv()) as process:
        ticks = process_start_ticks(process.pid)
        session.record_child(kernel.ChildIdentity(process.pid, process.pid, ticks), NOW)
        session.release_child(NOW)
        session.abandon()

        def process_is_live(pid: int, start_ticks: int) -> bool:
            checks.append((pid, start_ticks))
            return (pid, start_ticks) == (process.pid, ticks)

        recovery = replace(request, owner_pid=4301, owner_start_ticks=8301)

        # When: recovery attempts to claim cleanup authority and seal failure.
        with pytest.raises(c.IsolationError, match="live child"):
            _ = final_controller.acquire_final_wave(
                recovery, kernel.RecoveryPolicy(process_is_live)
            )

        # Then: the live identity remains nonterminal and cleanup is unverified.
        durable, _ = c.load_json(request.journal_path)
        assert durable["state"] in {"child-running", "recovering"}
        assert durable["cleanup_verified"] is False
        assert process.poll() is None
        assert (process.pid, ticks) in checks

    assert process.returncode is not None
