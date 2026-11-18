"""Thread fan-out with one runtime-role database connection per worker."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ops.perf.runtime import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(slots=True)
class WorkerResult:
    """Latencies in milliseconds and the failures one worker saw."""

    samples_ms: list[float] = field(default_factory=list)
    errors: int = 0


def run_workers(count: int, work: Callable[[int, WorkerResult], None]) -> WorkerResult:
    """Run ``work`` on ``count`` threads released together by a barrier.

    Each thread opens its own connection as ``clinic_app`` and closes it at
    the end. Worker exceptions propagate after every thread has stopped.
    """
    from django.db import connection  # noqa: PLC0415

    barrier = threading.Barrier(count)
    results = [WorkerResult() for _ in range(count)]
    failures: list[BaseException] = []

    def body(index: int) -> None:
        try:
            with runtime_role():
                barrier.wait()
                work(index, results[index])
        except BaseException as error:  # noqa: BLE001 - re-raised by the caller
            failures.append(error)
            barrier.abort()
        finally:
            connection.close()

    threads = [threading.Thread(target=body, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise failures[0]
    merged = WorkerResult()
    for item in results:
        merged.samples_ms.extend(item.samples_ms)
        merged.errors += item.errors
    return merged
