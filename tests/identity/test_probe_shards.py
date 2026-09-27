"""Sharding the probe matrix changes where a state runs, never what runs.

The partition places every state exactly once and keeps the phase tail
whole; a sharded run fails closed on a bad partition and on a worker that
crashes, raises, returns nothing or returns other states; it leaves no clone
and no connected backend, on success or failure; and a sharded run's
per-state verdicts equal the serial run's from the same database snapshot.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.db import connection
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from database_urls import database_url_for_name
from identity import (
    actor_channels,
    exemption_probes,
    probe_shards,
    probe_states,
    probe_worlds,
)
from identity.probe_states import ProbeState
from identity.probe_worlds import SYNTHETIC as _SYNTHETIC

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


_DB = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def fixed_verification_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.identity import stepup  # noqa: PLC0415 - as test_permission_parity

    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


def _states(count: int, tail: int = 0) -> tuple[ProbeState, ...]:
    """Synthetic states; the last ``tail`` carry a phase setup."""
    return tuple(
        ProbeState(
            f"state-{index}",
            "power",
            uuid4(),
            setup=(lambda: None) if index >= count - tail else None,
        )
        for index in range(count)
    )


def _statistics() -> None:
    """The parent's session as a probe world leaves it (workers match it)."""
    actor_channels.enable_function_statistics(
        database_url_for_name(
            os.environ["TEST_SUPERUSER_DATABASE_URL"],
            str(connection.settings_dict["NAME"]),
        )
    )


def _nothing_left() -> None:
    """No shard clone or backend on one, and no worker process (the test's
    own world clone is dropped at its teardown)."""
    assert probe_shards.leftovers() == []
    assert multiprocessing.active_children() == []


@pytest.mark.parametrize(
    ("count", "tail", "workers"),
    [(1413, 14, 4), (1413, 14, 3), (1413, 14, 1), (13, 3, 4), (5, 0, 4), (1, 0, 4)],
)
def test_partition_places_every_state_once(count: int, tail: int, workers: int) -> None:
    states = _states(count, tail)
    shards = probe_shards.partition(states, workers)
    probe_shards.check_partition(states, shards)
    assert len(shards) == min(workers, count)
    sizes = [len(shard) for shard in shards]
    assert max(sizes) - min(sizes) <= -(-count // len(shards))
    assert [state.label for shard in shards for state in shard] == [
        state.label for state in states
    ]


def test_the_worker_count_is_the_hosted_runner_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for cpus, workers in ((32, 4), (4, 4), (2, 2), (None, 1)):
        monkeypatch.setattr(os, "cpu_count", lambda cpus=cpus: cpus)
        assert probe_shards.worker_count() == workers


def _bad(change: str, states: Sequence[ProbeState]) -> list[tuple[ProbeState, ...]]:
    shards = list(probe_shards.partition(states, 4))
    if change == "missing":
        shards[1] = shards[1][1:]
    elif change == "duplicated":
        shards[2] = (*shards[2], shards[1][0])
    elif change == "empty":
        shards.append(())
    else:  # the phase tail split across two shards
        last = shards[-1]
        first = next(index for index, state in enumerate(last) if state.setup)
        shards[-2] = (*shards[-2], *last[: first + 1])
        shards[-1] = last[first + 1 :]
    return shards


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("missing", "misses"),
        ("duplicated", "more than one shard"),
        ("empty", "holds no state"),
        ("phase-split", "phase tail"),
    ],
)
def test_a_bad_partition_fails_closed(change: str, match: str) -> None:
    states = _states(13, 3)
    with pytest.raises(probe_shards.ShardError, match=match):
        probe_shards.check_partition(states, _bad(change, states))


def _current_database() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.current_database()")
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


@_DB
def test_every_shard_runs_on_its_own_clone_and_leaves_nothing() -> None:
    _statistics()
    source = _current_database()
    states = _states(8)

    def work(shard: tuple[ProbeState, ...]) -> tuple[tuple[str, ...], str]:
        return tuple(state.label for state in shard), _current_database()

    names = probe_shards.run_shards(states, probe_shards.partition(states, 4), work)

    assert len(set(names)) == 4
    assert all(name.startswith(probe_shards.CLONE_PREFIX) for name in names)
    assert _current_database() == source
    _nothing_left()


@_DB
@pytest.mark.parametrize(
    ("failure", "match"),
    [
        ("crash", "exit code -9"),
        ("exception", "synthetic-worker-failure"),
        ("zero-result", "ran 0 of its 2 states"),
        ("foreign", "ran 2 of its 2 states, or others"),
    ],
)
def test_a_failing_worker_fails_the_run_and_leaves_nothing(
    failure: str, match: str
) -> None:
    _statistics()
    states = _states(8)
    shards = probe_shards.partition(states, 4)
    victim = shards[1][0].label

    def work(shard: tuple[ProbeState, ...]) -> tuple[tuple[str, ...], None]:
        labels = tuple(state.label for state in shard)
        if shard[0].label != victim:
            return labels, None
        assert _current_database().startswith(probe_shards.CLONE_PREFIX)
        if failure == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        if failure == "exception":
            message = "synthetic-worker-failure"
            raise RuntimeError(message)
        if failure == "zero-result":
            return (), None
        return labels[::-1], None

    with pytest.raises(probe_shards.ShardError, match=match):
        probe_shards.run_shards(states, shards, work)
    _nothing_left()


# One pass over a world: the runs for the chosen states.
type Pass = Callable[
    [exemption_probes.ProbeWorld, Sequence[ProbeState]],
    dict[str, exemption_probes.ProbeRun],
]


def serial_pass(
    world: exemption_probes.ProbeWorld, states: Sequence[ProbeState]
) -> dict[str, exemption_probes.ProbeRun]:
    probes = [exemption_probes.PROBES[key] for key in sorted(exemption_probes.PROBES)]
    return exemption_probes.run_matrix(probes, world, states)


def sharded_pass(
    world: exemption_probes.ProbeWorld, states: Sequence[ProbeState]
) -> dict[str, exemption_probes.ProbeRun]:
    probes = [exemption_probes.PROBES[key] for key in sorted(exemption_probes.PROBES)]
    return exemption_probes.run_matrix(
        probes, world, states, workers=probe_shards.WORKERS
    )


def equivalent_runs(
    seeded_world: probe_worlds.SeededWorld,
    pick: Callable[[tuple[ProbeState, ...]], Sequence[ProbeState]],
    left: Pass = serial_pass,
    right: Pass = sharded_pass,
) -> tuple[Runs, Runs]:
    """Two passes over the same seeded world, each on its own fresh clone
    of the session template (identical database, deep-copied Python world);
    each pass's rows are read back where they were written."""
    results: list[Runs] = []
    with override_settings(**_SYNTHETIC):
        for run in (left, right):
            world = seeded_world()
            runs = run(world, pick(world.matrix.states))
            results.append((runs, probe_states.realized(world.matrix)))
    return results[0], results[1]


type Runs = tuple[dict[str, exemption_probes.ProbeRun], dict[str, set[str]]]


def verdicts(
    runs: dict[str, exemption_probes.ProbeRun],
) -> dict[str, tuple[object, ...]]:
    """Everything the certifier concludes from one run, per probe: every
    state's outcome and observations, the reach, and the problems. The
    deployed run (``baseline``) executes in the parent before any state in
    both modes and holds values minted per call (fresh row ids, times), so
    it enters as the class it reached, which the problems also check."""
    return {
        symbol: (
            list(run.outcomes.items()),
            sorted(run.observed.items()),
            None if run.baseline is None else exemption_probes.reached(run.baseline),
            sorted(run.not_entered),
            sorted(run.reached),
            exemption_probes.problems(exemption_probes.PROBES[symbol], run),
        )
        for symbol, run in runs.items()
    }


@_DB
def test_a_sharded_matrix_equals_the_serial_one_and_leaves_nothing(
    seeded_world: probe_worlds.SeededWorld,
) -> None:
    """A slice with every family kind, the removals' scoped changes and the
    whole phase tail: identical outcomes, observations, reach and problems;
    no clone and no backend is left once the matrix returns."""
    serial, sharded = equivalent_runs(
        seeded_world,
        lambda states: (*states[:5], *states[-118:-112], *states[-14:]),
    )
    assert verdicts(sharded[0]) == verdicts(serial[0])
    assert sharded[1] == serial[1]
    _nothing_left()


@_DB
def test_a_worker_killed_mid_matrix_or_a_dropped_state_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, seeded_world: probe_worlds.SeededWorld
) -> None:
    """A worker killed after its first state, and a partition that drops
    one state, fail the matrix; nothing is left behind either way."""
    with override_settings(**_SYNTHETIC):
        probe_world = seeded_world()
        probes = [
            exemption_probes.PROBES[key] for key in sorted(exemption_probes.PROBES)
        ]
        states = probe_world.matrix.states[:13]
        victim = probe_shards.partition(states[1:], 4)[1][0].label
        parent = os.getpid()
        run_state = exemption_probes._run_state

        def dying(state: ProbeState, *args: object, **kwargs: object) -> None:
            run_state(state, *args, **kwargs)  # type: ignore[arg-type]
            if os.getpid() != parent and state.label == victim:
                os.kill(os.getpid(), signal.SIGKILL)

        monkeypatch.setattr(exemption_probes, "_run_state", dying)
        with pytest.raises(probe_shards.ShardError, match="exit code -9"):
            exemption_probes.run_matrix(probes, probe_world, states, workers=4)
        _nothing_left()

        monkeypatch.setattr(exemption_probes, "_run_state", run_state)
        partition = probe_shards.partition

        def dropping(
            chosen: Sequence[ProbeState], workers: int
        ) -> tuple[tuple[ProbeState, ...], ...]:
            shards = list(partition(chosen, workers))
            shards[1] = shards[1][1:]
            return tuple(shards)

        monkeypatch.setattr(probe_shards, "partition", dropping)
        probe_world.matrix.applied.clear()
        probe_worlds.enable_statistics()  # the sharded run closed the connection
        with pytest.raises(probe_shards.ShardError, match="misses"):
            exemption_probes.run_matrix(probes, probe_world, states, workers=4)
        _nothing_left()
