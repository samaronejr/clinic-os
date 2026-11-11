"""Fairness stress: a noisy neighbour must not move the clinical save path.

The neighbour's load runs in separate OS processes so the measured autosave
threads never share a GIL with the load generator; the measured p95 then moves
only through the server (database, Redis), which is what fairness is about.
"""

from __future__ import annotations

import multiprocessing
import statistics
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID, uuid4

from ops.perf.children import ChildContext, child_setup, context_for
from ops.perf.profiles import DEFAULT_FAIRNESS_TRIALS
from ops.perf.report import fairness_record
from ops.perf.runtime import runtime_role
from ops.perf.scenarios import Prepared, autosave_run
from ops.perf.stats import percentile

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event
    from types import TracebackType

TRIALS = DEFAULT_FAIRNESS_TRIALS
IMPORT_BATCH = 200
IMPORT_PROCESSES = 1
FLOOD_PROCESSES = 2
VICTIM_PROBES = 40
WARM_TIMEOUT_SECONDS = 120
DEFER_BACKOFF_SECONDS = 0.05


def _import_child(  # noqa: PLR0913 - one load process's shared handles
    context: ChildContext,
    index: int,
    stop: Event,
    warm: Event,
    counts: Any,  # noqa: ANN401 - multiprocessing shared array
    failed: Event,
) -> None:
    """Write import-shaped batches of encrypted registrations until stopped."""
    try:
        child_setup(context)
        from apps.core.fairness import fair_acquire  # noqa: PLC0415
        from apps.intake.patient_creation import create_patient  # noqa: PLC0415
        from apps.tenancy.db import tenant_context  # noqa: PLC0415

        with runtime_role():
            iteration = 0
            while not stop.is_set():
                # The real bulk task spends one token of the organization's
                # ``bulk`` bucket per batch and is redelivered when deferred.
                if not fair_acquire(
                    organization_id=UUID(context.organization_id), queue="bulk"
                ):
                    with counts.get_lock():
                        counts[1] += 1
                    stop.wait(DEFER_BACKOFF_SECONDS)
                    continue
                # One import-shaped batch: many encrypted registrations in one
                # transaction, the write pattern an import commit batch has.
                with tenant_context(
                    UUID(context.admin_id), UUID(context.organization_id)
                ):
                    for row in range(IMPORT_BATCH):
                        create_patient(
                            clinic_id=UUID(context.clinic_id),
                            full_name=f"Importado Sintetico {index}-{iteration}-{row}",
                            birth_date=None,
                            idempotency_key=uuid4(),
                        )
                with counts.get_lock():
                    counts[0] += IMPORT_BATCH
                iteration += 1
                warm.set()
    except BaseException:
        failed.set()
        warm.set()
        raise


def _flood_child(
    context: ChildContext,
    _index: int,
    stop: Event,
    warm: Event,
    counts: Any,  # noqa: ANN401 - multiprocessing shared array
    failed: Event,
) -> None:
    """Request ai-batch admission in a tight loop until stopped."""
    try:
        child_setup(context)
        from apps.core.fairness import fair_acquire  # noqa: PLC0415

        with runtime_role():
            while not stop.is_set():
                admitted = fair_acquire(
                    organization_id=UUID(context.organization_id), queue="ai-batch"
                )
                with counts.get_lock():
                    counts[0 if admitted else 1] += 1
                warm.set()
    except BaseException:
        failed.set()
        warm.set()
        raise


class BackgroundLoad:
    """Run load processes until exit; wait until each completed one step."""

    def __init__(self, target: Any, processes: int, context: ChildContext) -> None:  # noqa: ANN401
        """Prepare the shared state; nothing starts until ``__enter__``."""
        spawn = multiprocessing.get_context("spawn")
        self.spawn = spawn
        self.stop = spawn.Event()
        self.failed = spawn.Event()
        self.warm = [spawn.Event() for _ in range(processes)]
        self.counts = spawn.Array("q", [0, 0])
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
        for child in self.children:
            child.start()
        for event in self.warm:
            if not event.wait(WARM_TIMEOUT_SECONDS) or self.failed.is_set():
                self.__exit__(None, None, None)
                message = "background load did not start"
                raise RuntimeError(message)
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Stop and join every process; a failed load invalidates the run."""
        self.stop.set()
        for child in self.children:
            child.join(WARM_TIMEOUT_SECONDS)
            if child.is_alive():
                child.terminate()
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
        base = _p95(prepared, saves)
        with make_load() as load:
            under = _p95(prepared, saves)
            detail["during"] = during(load)
        baselines.append(base)
        loaded.append(under)
        detail["trials_ms"].append(
            {"baseline_p95": round(base, 3), "loaded_p95": round(under, 3)}
        )
    return statistics.median(baselines), statistics.median(loaded), detail


def chart_save_vs_import(prepared: Prepared, *, trials: int = TRIALS) -> dict[str, Any]:
    """Chart-save p95 while a bulk import writes to the same tenant."""
    context = _context(prepared, as_neighbor=False)
    base, under, detail = _alternate(
        prepared,
        trials,
        lambda: BackgroundLoad(_import_child, IMPORT_PROCESSES, context),
        lambda load: {
            "import_processes": IMPORT_PROCESSES,
            "import_batch_rows": IMPORT_BATCH,
            "import_rows_written_so_far": load.first,
            "import_batches_deferred_by_bucket": load.second,
        },
    )
    return fairness_record(
        fairness_id="chart_save_during_bulk_import",
        baseline_p95_ms=base,
        loaded_p95_ms=under,
        trials=trials,
        load=detail,
        also_required=detail["during"]["import_rows_written_so_far"] > 0,
        notes="median p95 over alternating baseline/loaded trials after one "
        "warm-up; the load is import-shaped batches of create_patient "
        "registrations in separate processes on the same tenant, not the "
        "interop job machinery",
    )


def ai_batch_flood(prepared: Prepared, *, trials: int = TRIALS) -> dict[str, Any]:
    """One tenant floods ai-batch; another tenant's clinical saves must hold."""
    from apps.core.fairness import fair_acquire  # noqa: PLC0415

    focus = prepared.dataset.focus
    context = _context(prepared, as_neighbor=True)
    victim = {"requested": 0, "admitted": 0}

    def during(load: BackgroundLoad) -> dict[str, Any]:
        for _ in range(VICTIM_PROBES):
            victim["requested"] += 1
            if fair_acquire(organization_id=focus.organization_id, queue="ai-batch"):
                victim["admitted"] += 1
        return {
            "flood_processes": FLOOD_PROCESSES,
            "flooder_admitted": load.first,
            "flooder_deferred": load.second,
        }

    with runtime_role():
        base, under, detail = _alternate(
            prepared,
            trials,
            lambda: BackgroundLoad(_flood_child, FLOOD_PROCESSES, context),
            during,
        )
    detail["during"]["victim_ai_batch_requested"] = victim["requested"]
    detail["during"]["victim_ai_batch_admitted"] = victim["admitted"]
    held = victim["admitted"] == victim["requested"]
    throttled = detail["during"]["flooder_deferred"] > 0
    return fairness_record(
        fairness_id="clinical_latency_during_ai_batch_flood",
        baseline_p95_ms=base,
        loaded_p95_ms=under,
        trials=trials,
        load=detail,
        also_required=held and throttled,
        notes="the flooding tenant must be deferred by its own token bucket "
        "while the victim tenant is admitted on ai-batch and its autosave p95 "
        "stays within tolerance; the flood runs in separate processes",
    )
