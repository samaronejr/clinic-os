"""Scoped transport queues and memory accounting for synthetic load."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

IMPORT_TASK = "apps.interop.tasks.commit_batch"
QUEUE_PREFIX = "clinic-perf-import-"


def _check_queue(name: str) -> None:
    if re.fullmatch(r"clinic-perf-import-[0-9a-f]{32}", name) is None:
        message = "not a scoped performance import queue"
        raise ValueError(message)


@contextmanager
def import_queue(name: str) -> Iterator[None]:
    """Route real import publications to this workload's private queue."""
    from config.celery import app  # noqa: PLC0415

    _check_queue(name)
    previous = app.conf.task_routes
    app.conf.update(task_routes=({IMPORT_TASK: {"queue": name}}, previous))
    try:
        yield
    finally:
        app.conf.update(task_routes=previous)


def clear_import_queue(name: str) -> int:
    """Remove only the stopped workload's unused real broker copies."""
    from config.celery import app  # noqa: PLC0415

    _check_queue(name)
    with app.connection_for_write() as connection:
        queue = app.amqp.queues[name](connection.default_channel)
        removed = int(queue.purge())
        queue.delete(if_empty=True)
    return removed


def broker_memory() -> dict[str, int]:
    """Read actual server memory; no broker address or credential is reported."""
    from django.conf import settings  # noqa: PLC0415
    from redis import Redis  # noqa: PLC0415

    with Redis.from_url(str(settings.CELERY_BROKER_URL)) as client:
        info = client.info("memory")
    return {
        "used_memory_peak_bytes": int(info["used_memory_peak"]),
        "used_memory_rss_bytes": int(info["used_memory_rss"]),
    }
