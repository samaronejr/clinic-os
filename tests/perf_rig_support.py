"""Test-only process placement for real performance scenario workers."""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.context import SpawnProcess
from typing import TYPE_CHECKING
from unittest.mock import patch

from ops.perf.fairness_runs import WARM_TIMEOUT_SECONDS, BackgroundLoad
from ops.perf.import_load import import_child

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import SynchronizedArray
    from multiprocessing.synchronize import Event
    from types import TracebackType

    from ops.perf.import_load import ImportContext


class UnconfinedProcess(SpawnProcess):
    """Keep spawned worker bodies real without requiring delegated cgroups."""

    def run(self) -> None:
        """Replace only the workers' process-placement calls."""
        with (
            patch("ops.perf.ai_load.attach"),
            patch("ops.perf.import_load.attach"),
        ):
            super().run()


@dataclass(frozen=True)
class _GatedImportChild:
    release: Event
    progressed: Event

    def __call__(
        self,
        context: ImportContext,
        index: int,
        stop: Event,
        warm: Event,
        *state: *tuple[SynchronizedArray[int], Event],
    ) -> None:
        counts, failed = state
        signal_warm = warm.set

        def signal_commit() -> None:
            already_warm = warm.is_set()
            signal_warm()
            # The real child signals only after updating the committed row count,
            # or on failure (which the parent checks separately).
            if already_warm or failed.is_set():
                self.progressed.set()
            elif not self.release.wait(WARM_TIMEOUT_SECONDS):
                message = "measured import gate was not released"
                raise TimeoutError(message)

        with patch.object(warm, "set", signal_commit):
            import_child(context, index, stop, warm, counts, failed)


class GatedImportLoad(BackgroundLoad):
    """Pause a real import after its first committed batch, before the next."""

    def __init__(self, context: ImportContext, *, processes: int) -> None:
        spawn = get_context("spawn")
        self.release = spawn.Event()
        self.progressed = spawn.Event()
        target = _GatedImportChild(self.release, self.progressed)
        super().__init__(target, processes, context)

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        # Withheld/aborted trials must also exit without an extra commit.
        self.stop.set()
        self.release.set()
        super().__exit__(kind, error, trace)
