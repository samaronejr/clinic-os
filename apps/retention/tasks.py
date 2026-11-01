"""Reference-only durable purge dispatch; provider and live legs remain blocked."""

from functools import partial
from typing import Protocol
from uuid import UUID

from celery import shared_task
from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.db import connections, transaction

from apps.retention.execution import (
    TRANSIENT_CLASSES,
    RetentionExecutionDeniedError,
    run_purge,
)
from apps.retention.services import apply_retention_policy
from apps.tenancy.db import service_principal_context


class _TaskSender(Protocol):
    """Publish only references through the registered Celery task."""

    def apply_async(self, *, kwargs: dict[str, str], queue: str) -> None: ...


def run_policy(*, clinic_id: str, principal_id: str, record_class: str) -> None:
    """Run one scheduled clinic sweep with no tenant or human actor claim."""
    clinic = UUID(clinic_id)
    with service_principal_context(principal_id=UUID(principal_id), clinic_id=clinic):
        apply_retention_policy(clinic_id=clinic, record_class=record_class)


def run_artifact(*, clinic_id: str, principal_id: str, purge_id: str) -> None:
    """Recheck the registered retention principal before each destructive step."""
    run_purge(
        clinic_id=UUID(clinic_id),
        principal_id=UUID(principal_id),
        purge_id=UUID(purge_id),
    )


def scan_due() -> int:
    """Repeat sweeps only for retention principals bound to this worker's login."""
    if resolved_data_mode() != SYNTHETIC_DATA_MODE:
        raise RetentionExecutionDeniedError
    if "agent" not in connections:
        return 0
    with connections["agent"].cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.retention_sweep_scopes()")
        scopes = cursor.fetchall()
    for principal_id, clinic_id in scopes:
        for record_class in TRANSIENT_CLASSES:
            transaction.on_commit(
                partial(
                    apply_policy.apply_async,
                    kwargs={
                        "clinic_id": str(clinic_id),
                        "principal_id": str(principal_id),
                        "record_class": record_class,
                    },
                    queue="bulk",
                ),
                using="agent",
            )
    return len(scopes) * len(TRANSIENT_CLASSES)


scan_due_job = shared_task(
    name="apps.retention.tasks.scan_due",
    acks_late=True,
    reject_on_worker_lost=True,
)(scan_due)
apply_policy: _TaskSender = shared_task(
    name="apps.retention.tasks.apply_policy",
    acks_late=True,
    reject_on_worker_lost=True,
)(run_policy)
purge_artifact: _TaskSender = shared_task(
    name="apps.retention.tasks.purge_artifact",
    acks_late=True,
    reject_on_worker_lost=True,
)(run_artifact)
