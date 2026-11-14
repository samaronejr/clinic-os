"""Fairness stress: a noisy neighbour must not move the clinical save path.

External producers run outside the fixed application budget. Actual admission
and bulk work run in the worker share; clinical work keeps its isolated share.
PostgreSQL and Redis stay shared and their contention remains in the result.
"""

from __future__ import annotations

import multiprocessing
import statistics
import time
from typing import TYPE_CHECKING, Any, Self

from ops.perf.ai_load import PRODUCERS, AdmissionLoad
from ops.perf.broker import clear_import_queue
from ops.perf.children import ChildContext, context_for, wait_for_start
from ops.perf.import_load import (
    IMPORT_ROWS,
    ImportContext,
    import_child,
    prepare_import,
)
from ops.perf.profiles import DEFAULT_FAIRNESS_TRIALS
from ops.perf.report import fairness_record
from ops.perf.rig import resource_snapshot
from ops.perf.runtime import runtime_role
from ops.perf.scenarios import Prepared, autosave_run
from ops.perf.stats import percentile

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess
    from types import TracebackType

TRIALS = DEFAULT_FAIRNESS_TRIALS
IMPORT_PROCESSES = 1
FLOOD_PROCESSES = PRODUCERS
VICTIM_PROBES = 40
WARM_TIMEOUT_SECONDS = 120


class BackgroundLoad:
    """Run load processes until exit; wait until each completed one step."""

    def __init__(
        self,
        target: Any,  # noqa: ANN401 - a spawn-compatible workload entrypoint
        processes: int,
        context: ChildContext | ImportContext,
    ) -> None:
        """Prepare the shared state; nothing starts until ``__enter__``."""
        spawn = multiprocessing.get_context("spawn")
        self.spawn = spawn
        self.context = context
        self.stop = spawn.Event()
        self.failed = spawn.Event()
        self.warm = [spawn.Event() for _ in range(processes)]
        self.counts = spawn.Array("q", [0, 0])
        self.started: list[BaseProcess] = []
        self.children = [
            spawn.Process(
                target=target,
                args=(
                    context,
                    index,
                    self.stop,
                    self.warm[index],
                    self.counts,
                    self.failed,
                ),
            )
            for index in range(processes)
        ]

    def __enter__(self) -> Self:
        """Start the processes and wait until each wrote or requested once."""
        try:
            for child in self.children:
                child.start()
                self.started.append(child)
            wait_for_start(self.warm, self.failed, WARM_TIMEOUT_SECONDS)
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Stop and join every process; a failed load invalidates the run."""
        self.stop.set()
        for child in self.started:
            child.join(WARM_TIMEOUT_SECONDS)
            if child.is_alive():
                self.failed.set()
                child.terminate()
                child.join()
            if child.exitcode != 0:
                self.failed.set()
        if isinstance(self.context, ImportContext):
            clear_import_queue(self.context.queue_name)
        if self.failed.is_set() and error is None:
            message = "background load process failed"
            raise RuntimeError(message)

    @property
    def first(self) -> int:
        """Return the first counter (admitted or written rows)."""
        return int(self.counts[0])

    @property
    def second(self) -> int:
        """Return the second counter (deferred requests)."""
        return int(self.counts[1])


def _context(prepared: Prepared, *, as_neighbor: bool) -> ChildContext:
    tenant = prepared.dataset.neighbor if as_neighbor else prepared.dataset.focus
    return context_for(tenant)


def _p95(prepared: Prepared, saves: int) -> float:
    result = autosave_run(prepared, saves_per_editor=saves)
    if result.errors:
        message = "autosave fairness sample contains failed saves"
        raise RuntimeError(message)
    return percentile(result.samples_ms, 0.95)


def _saves(prepared: Prepared) -> int:
    return max(1, prepared.dataset.profile.samples // len(prepared.drafts))


def _alternate(
    prepared: Prepared,
    trials: int,
    make_load: Any,  # noqa: ANN401 - a zero-argument BackgroundLoad factory
    during: Any,  # noqa: ANN401 - callback run while the load is active
) -> tuple[float, float, dict[str, Any]]:
    saves = _saves(prepared)
    _p95(prepared, saves)  # discarded warm-up: caches, pools, JIT-free paths
    baselines: list[float] = []
    loaded: list[float] = []
    detail: dict[str, Any] = {"trials_ms": []}
    for _ in range(trials):
        baseline_started = time.perf_counter()
        base = _p95(prepared, saves)
        baseline_seconds = time.perf_counter() - baseline_started
        with make_load() as load:
            before_resources = resource_snapshot()
            loaded_started = time.perf_counter()
            before = load.first
            before_deferred = load.second
            under = _p95(prepared, saves)
            completed = load.first - before
            deferred = load.second - before_deferred
            loaded_seconds = time.perf_counter() - loaded_started
            after_resources = resource_snapshot()
            detail["during"] = during(load)
            detail["during"]["work_completed_during_saves"] = completed
        baselines.append(base)
        loaded.append(under)
        detail["trials_ms"].append(
            {
                "baseline_p95": round(base, 3),
                "loaded_p95": round(under, 3),
                "baseline_window_seconds": baseline_seconds,
                "loaded_window_seconds": loaded_seconds,
                "work_completed_during_saves": completed,
                "work_deferred_during_saves": deferred,
                "resource_cpu_seconds": {
                    role: round(
                        (
                            int(values["cpu_usage_usec"])
                            - int(before_resources[role]["cpu_usage_usec"])
                        )
                        / 1_000_000,
                        6,
                    )
                    for role, values in after_resources.items()
                },
            }
        )
    return statistics.median(baselines), statistics.median(loaded), detail


def chart_save_vs_import(prepared: Prepared, *, trials: int = TRIALS) -> dict[str, Any]:
    """Chart-save p95 while a bulk import writes to the same tenant."""
    batch_sizes: list[int] = []

    def make_load() -> BackgroundLoad:
        context = prepare_import(prepared.dataset.focus)
        batch_sizes.append(max(rows for _, rows in context.batches))
        return BackgroundLoad(import_child, IMPORT_PROCESSES, context)

    base, under, detail = _alternate(
        prepared,
        trials,
        make_load,
        lambda load: {
            "entrypoint": "apps.interop.tasks.commit_batch",
            "import_processes": IMPORT_PROCESSES,
            "import_source_rows": IMPORT_ROWS,
            "import_batch_rows": batch_sizes[-1],
            "import_rows_written_so_far": load.first,
            "import_batches_deferred": load.second,
        },
    )
    return fairness_record(
        fairness_id="chart_save_during_bulk_import",
        baseline_p95_ms=base,
        loaded_p95_ms=under,
        trials=trials,
        load=detail,
        also_required=all(
            trial["work_completed_during_saves"] > 0 for trial in detail["trials_ms"]
        ),
        notes="median p95 over alternating baseline/loaded trials after one "
        "warm-up; the load uploads, maps, validates and approves a governed "
        "import, then invokes its bulk task entrypoints in a separate process on "
        "the same tenant; every batch owns its tenant transaction, quota and "
        "busy-chain deferral; every trial commits rows during the save phase",
    )


def ai_batch_flood(prepared: Prepared, *, trials: int = TRIALS) -> dict[str, Any]:
    """One tenant floods ai-batch; another tenant's clinical saves must hold."""
    focus = prepared.dataset.focus
    context = _context(prepared, as_neighbor=True)
    victim = {"requested": 0, "admitted": 0}

    def during(load: AdmissionLoad) -> dict[str, Any]:
        for _ in range(VICTIM_PROBES):
            victim["requested"] += 1
            if load.victim_admitted():
                victim["admitted"] += 1
        return {
            "flood_processes": FLOOD_PROCESSES,
            "admission_location": "worker resource share",
            "producer_location": "outside application resource parent",
            "flooder_admitted": load.first,
            "flooder_deferred": load.second,
        }

    with runtime_role():
        base, under, detail = _alternate(
            prepared,
            trials,
            lambda: AdmissionLoad(context, context_for(focus)),
            during,
        )
    for trial in detail["trials_ms"]:
        trial["admitted"] = trial["work_completed_during_saves"]
        trial["deferred"] = trial["work_deferred_during_saves"]
        trial["offered"] = trial["admitted"] + trial["deferred"]
        trial["completed_response_rate_per_second"] = (
            trial["offered"] / trial["loaded_window_seconds"]
        )
    detail["totals"] = {
        key: sum(trial[key] for trial in detail["trials_ms"])
        for key in ("offered", "admitted", "deferred")
    }
    detail["during"]["victim_ai_batch_requested"] = victim["requested"]
    detail["during"]["victim_ai_batch_admitted"] = victim["admitted"]
    held = victim["admitted"] == victim["requested"]
    active_flood = all(
        trial["admitted"] > 0 and trial["deferred"] > 0 for trial in detail["trials_ms"]
    )
    return fairness_record(
        fairness_id="clinical_latency_during_ai_batch_flood",
        baseline_p95_ms=base,
        loaded_p95_ms=under,
        trials=trials,
        load=detail,
        also_required=held and active_flood,
        notes="the flooding tenant must be deferred by its own token bucket "
        "while the victim tenant is admitted on ai-batch and its autosave p95 "
        "stays within tolerance; two unbounded external producers call real "
        "admission in the fixed AI worker share; producer CPU is reported "
        "separately and lower offered load is not evidence of isolation; "
        "offered counts completed admitted/deferred responses, and its "
        "per-window rate is closed-loop completion throughput",
    )
