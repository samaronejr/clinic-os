"""Real Celery worker app for the ADR-014 worker-logging test.

Started as ``celery -A tests.core.celery_worker_probe worker`` by
``test_real_celery_worker_logs_only_through_the_allowlist``. It reuses the
production ``config.celery.app`` (and therefore its logging configuration),
uses the private filesystem transport chosen by the test, registers two probe
tasks and reports lifecycle events through a FIFO so the test waits on
exact events instead of timers.
"""

from __future__ import annotations

import json
import logging
import os

from celery import signals
from config.celery import app

PROBE_QUEUE = "probe"

# The test owns the transport: it passes the exact broker URL and transport
# options (a private runtime directory) so producer and worker share them.
app.conf.update(
    broker_transport_options=json.loads(os.environ["CLINIC_PROBE_TRANSPORT_OPTIONS"]),
    task_default_queue=PROBE_QUEUE,
)
# CELERY_BROKER_URL outranks conf; refuse anything but the private transport.
if app.conf.broker_url != "filesystem://":
    message = "probe worker requires CELERY_BROKER_URL=filesystem://"
    raise RuntimeError(message)
_EVENTS_FIFO = os.environ["CLINIC_PROBE_EVENTS_FIFO"]
logger = logging.getLogger(__name__)


def phi_echo(body: str, *, note: str) -> str:
    """Log, print and return clinical input through every worker path."""
    logger.warning("Forbidden: %s", body)
    logger.info("http request", extra={"reason_code": note, "event": note})
    print(body)  # noqa: T201 - proves task stdout is routed through logging
    return body


def phi_raise(body: str, *, note: str) -> None:
    """Fail with clinical text in the exception message."""
    raise ValueError(body + note)


app.task(name="probe.phi_echo")(phi_echo)
app.task(name="probe.phi_raise")(phi_raise)


def _emit_event(event: str) -> None:
    descriptor = os.open(_EVENTS_FIFO, os.O_WRONLY)
    try:
        os.write(descriptor, f"{event}\n".encode())
    finally:
        os.close(descriptor)


class _TraceEventHandler(logging.Handler):
    """Signal each ``celery.app.trace`` record after it was written.

    Installed on the root logger after the allowlisted console handler, so
    the record is already flushed to stderr when the test is released.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "celery.app.trace":
            _emit_event(record.levelname.lower())


def _on_worker_ready(**_kwargs: object) -> None:
    logging.getLogger().addHandler(_TraceEventHandler())
    _emit_event("ready")


signals.worker_ready.connect(_on_worker_ready, weak=False)
