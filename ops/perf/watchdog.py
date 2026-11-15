"""Fail closed with all Python thread stacks when a measured stage stalls."""

from __future__ import annotations

import faulthandler
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
    from typing import TextIO

DEFAULT_STAGE_SECONDS = 900.0
_T = TypeVar("_T")
_CURRENT: ContextVar[StageWatchdog | None] = ContextVar("perf_watchdog", default=None)


class StageWatchdog:
    """Own one native faulthandler timer across sequential runner stages."""

    def __init__(
        self, output: TextIO | None = None, *, seconds: float = DEFAULT_STAGE_SECONDS
    ) -> None:
        """Keep the evidence descriptor open until every stage has stopped."""
        self.output = output if output is not None else sys.stderr
        self.traceback_output = output if output is not None else 2
        self.seconds = seconds

    @classmethod
    def current(cls) -> StageWatchdog:
        """Use the CLI's evidence stream for calls through the public Python API."""
        current = _CURRENT.get()
        return current if current is not None else cls()

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Share this invocation's watchdog without changing workload signatures."""
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Dump every thread and exit 1 even when Python cannot unwind a wait."""
        self.output.write(f"perf stage: {name} started deadline={self.seconds:g}s\n")
        self.output.flush()
        faulthandler.dump_traceback_later(
            self.seconds, file=self.traceback_output, exit=True
        )
        completed = False
        try:
            yield
            completed = True
        finally:
            faulthandler.cancel_dump_traceback_later()
            outcome = "finished" if completed else "failed"
            self.output.write(f"perf stage: {name} {outcome}\n")
            self.output.flush()

    @contextmanager
    def context(self, name: str, manager: AbstractContextManager[_T]) -> Iterator[_T]:
        """Give setup and teardown independent deadlines without nested timers."""
        with self.stage(name):
            value = manager.__enter__()
        try:
            yield value
        except BaseException as error:
            with self.stage(f"{name}.cleanup"):
                suppressed = manager.__exit__(type(error), error, error.__traceback__)
            if not suppressed:
                raise
        else:
            with self.stage(f"{name}.cleanup"):
                manager.__exit__(None, None, None)
