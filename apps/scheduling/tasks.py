"""Celery entrypoint for the W (machine) hold-expiry job (todo 22)."""

import os

from celery import shared_task
from ops.release.activation import require_live_runtime

from apps.scheduling.appointment_lifecycle import expire_due_holds


@shared_task(name="apps.scheduling.tasks.expire_holds")  # type: ignore[untyped-decorator]
def expire_holds() -> int:
    """Expire due holds as the machine actor; bookings also expire them lazily.

    The worker holds no human actor or patient setting. Each hold expires in its
    own transaction with a deterministic command, so a duplicate beat run
    replays instead of double-writing, and a hold booked meanwhile is skipped.
    """
    require_live_runtime(os.environ)
    return len(expire_due_holds())
