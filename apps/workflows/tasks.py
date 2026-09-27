"""Minute-resolution durable scanner and Celery entrypoints on the bulk queue."""

from __future__ import annotations

import os
from uuid import UUID

from celery import shared_task
from django.db import connection
from ops.release.activation import require_live_runtime

from apps.identity.current_context import CurrentActorError
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.workflows import engine
from apps.workflows.task_services import escalate_task


def execute_step(*, step_id: str) -> str:
    """Execute only stored step scope; a queued message carries no authority."""
    require_live_runtime(os.environ)
    return engine.execute_step(step_id=UUID(step_id))


execute_step_job = shared_task(name="apps.workflows.tasks.execute_step")(execute_step)


def escalate(*, task_id: str) -> bool:
    """Resolve the stored creator, then recheck current authority for escalation."""
    require_live_runtime(os.environ)
    identifier = UUID(task_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT organization_id,clinic_id,actor_id "
            "FROM clinic_app.workflows_task_scope(%s)",
            [identifier],
        )
        scope = cursor.fetchone()
    if scope is None:
        return False
    try:
        with tenant_context(scope[2], scope[0]):
            return escalate_task(clinic_id=scope[1], task_id=identifier)
    except (CurrentActorError, TenantAccessDeniedError):
        return False


escalate_job = shared_task(name="apps.workflows.tasks.escalate")(escalate)


def scan_due() -> int:
    """Publish opaque committed IDs; duplicate scans share the worker claim lock."""
    require_live_runtime(os.environ)
    identifiers = engine.due_steps()
    for identifier in identifiers:
        execute_step_job.apply_async(kwargs={"step_id": str(identifier)})
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.workflows_due_tasks(%s)", [engine.utc_now()]
        )
        tasks = tuple(row[0] for row in cursor.fetchall())
    for identifier in tasks:
        escalate_job.apply_async(kwargs={"task_id": str(identifier)})
    return len(identifiers) + len(tasks)


scan_due_job = shared_task(name="apps.workflows.tasks.scan_due")(scan_due)
