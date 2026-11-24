"""Attach test observations to the production app without wrapping its tasks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from celery.signals import task_postrun, worker_process_init
from config.celery import app
from redis import Redis

if TYPE_CHECKING:
    from celery import Task

__all__ = ("app",)


def _compensate(*, clinic_id: str, operation_id: str, principal_id: str) -> str:
    from resilience.compensation_death_provider import run_compensation  # noqa: PLC0415

    return run_compensation(
        clinic_id=clinic_id, operation_id=operation_id, principal_id=principal_id
    )


app.task(name="gate.compensate")(_compensate)


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
    if task.name not in {
        "comms.execute_operation",
        "prescription.dispatch_signature",
        "gate.compensate",
    }:
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
    if "CLINIC_GATE_CANCEL_LEDGER" in os.environ:
        from resilience.compensation_death_provider import (  # noqa: PLC0415
            cancel,
            lookup,
        )

        patch("apps.comms.agent_reminders.cancel_reminder", cancel).start()
        patch("apps.comms.agent_reminders.reconcile_reminder", lookup).start()
    if ledger is not None:
        from resilience.broker_death_provider import install  # noqa: PLC0415

        install(Path(ledger))


task_postrun.connect(observe_delivery, weak=False)
worker_process_init.connect(install_provider_fakes, weak=False)
