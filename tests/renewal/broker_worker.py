"""Attach test observations to the production app without wrapping its tasks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from celery.signals import task_postrun, worker_process_init
from config.celery import app
from redis import Redis

if TYPE_CHECKING:
    from celery import Task

__all__ = ("app",)


def observe_delivery(
    *,
    task: Task,
    task_id: str,
    kwargs: dict[str, str],
    retval: object,
    state: str,
    **_: object,
) -> None:
    """Signal a committed local outcome and the transport's redelivery bit."""
    if task.name not in {"comms.execute_operation", "prescription.dispatch_signature"}:
        return
    event = [
        task_id,
        task.request.delivery_info.get("redelivered", False),
        retval if state == "SUCCESS" else state,
    ]
    with Redis.from_url(os.environ["CELERY_BROKER_URL"]) as redis:
        redis.rpush(f"completed:{kwargs['operation_id']}", json.dumps(event))


def install_provider_fakes(**_: object) -> None:
    """Attach crashable providers only in the two cut-point tests."""
    ledger = os.environ.get("CLINIC_GATE_EFFECT_LEDGER")
    if ledger is not None:
        from resilience.broker_death_provider import install  # noqa: PLC0415

        install(Path(ledger))


task_postrun.connect(observe_delivery, weak=False)
worker_process_init.connect(install_provider_fakes, weak=False)
