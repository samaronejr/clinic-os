"""Celery application for the shared integration job boundary.

Dispatch happens only through ``transaction.on_commit`` after the tenant
transaction commits, so rolled-back work never reaches a worker. Workers
acknowledge tasks late and requeue on worker loss; the stored operation row
bounds attempts and makes repeated deliveries idempotent.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("clinic")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(
    task_acks_late=True,
    task_default_queue="clinic-integrations",
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
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
