"""Celery application for the shared integration job boundary.

Dispatch happens only through ``transaction.on_commit`` after the tenant
transaction commits, so rolled-back work never reaches a worker. Workers
acknowledge tasks late and requeue on worker loss; the stored operation row
bounds attempts and makes repeated deliveries idempotent.

The queue topology isolates workloads so one tenant's bulk or AI burst
cannot delay another tenant's clinical work (IS-16): ``clinical`` stays
isolated and fail-open, while ``ai-batch`` and ``bulk`` are metered by the
per-organization token buckets in ``apps.core.fairness``. Routes match task
name prefixes; autodiscovered tasks are named by module path, so the
``apps.<app>.tasks.*`` entries route each domain's future tasks, and the
explicit ``comms.*`` names keep the existing outbox tasks on the unchanged
``clinic-integrations`` queue.
"""

import logging
import logging.config
import os

from celery import Celery
from celery.signals import setup_logging
from kombu import Queue  # type: ignore[import-untyped]

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

# Worker-side log formats never include the message: they only apply if
# Celery ever configures logging itself, which the receiver below prevents.
_SAFE_WORKER_LOG_FORMAT = "[%(asctime)s: %(levelname)s/%(processName)s] %(name)s"

app = Celery("clinic")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(
    task_acks_late=True,
    task_default_queue="clinic-integrations",
    task_queues=(
        Queue("clinic-integrations"),
        Queue("clinical"),
        Queue("ai-interactive"),
        Queue("ai-batch"),
        Queue("messaging"),
        Queue("finance"),
        Queue("bulk"),
    ),
    task_routes={
        # Existing comms outbox tasks keep their queue; never renamed.
        "comms.*": {"queue": "clinic-integrations"},
        # Future domain tasks route by module path (autodiscovered name).
        "apps.ehr.tasks.*": {"queue": "clinical"},
        "apps.prescription.tasks.*": {"queue": "clinical"},
        "apps.scribe.tasks.*": {"queue": "ai-interactive"},
        "apps.ai.tasks.*": {"queue": "ai-batch"},
        "apps.comms.tasks.*": {"queue": "messaging"},
        "apps.billing.tasks.*": {"queue": "finance"},
        "apps.insurance.tasks.*": {"queue": "finance"},
        "apps.subscriptions.tasks.*": {"queue": "finance"},
        "apps.retention.tasks.*": {"queue": "bulk"},
        "apps.workflows.tasks.*": {"queue": "bulk"},
        "apps.analytics.tasks.*": {"queue": "bulk"},
        "apps.crm.tasks.*": {"queue": "bulk"},
    },
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    worker_log_format=_SAFE_WORKER_LOG_FORMAT,
    worker_task_log_format=_SAFE_WORKER_LOG_FORMAT,
    beat_schedule={
        "appointment-reminders": {
            "task": "comms.dispatch_due_reminders",
            "schedule": 60.0,
        },
        "pending-operation-recovery": {
            "task": "comms.recover_pending_operations",
            "schedule": 60.0,
        },
    },
)
app.autodiscover_tasks()


def configure_worker_logging(**_kwargs: object) -> None:
    """Route every worker log through the ADR-014 allowlist handler.

    Connecting ``setup_logging`` stops Celery from installing its own
    root/task handlers, whose formats interpolate task args, kwargs and
    return values. The Django ``LOGGING`` dict (JSON formatter + allowlist
    filter) is applied instead, and task stdout/stderr is redirected into
    that same pipeline so ``print`` output cannot bypass it.
    """
    from django.conf import settings  # noqa: PLC0415

    logging.config.dictConfig(settings.LOGGING)
    # The level must be a *name*: Celery exports it as
    # CELERY_LOG_REDIRECT_LEVEL and every prefork child re-applies it through
    # mlevel(), which rejects digit strings ("30") and kills the pool.
    app.log.redirect_stdouts(loglevel=app.conf.worker_redirect_stdouts_level)


setup_logging.connect(
    configure_worker_logging,
    dispatch_uid="config.celery.configure_worker_logging",
)
