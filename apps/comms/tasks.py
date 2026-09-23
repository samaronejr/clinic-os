"""Celery entrypoints for the comms integration boundary."""

import os
from uuid import UUID

from celery import shared_task
from django.db import connection
from ops.release.activation import require_live_runtime

from apps.core.integration import ExecutionResult
from apps.core.integration import execute_operation as _execute


@shared_task(name="comms.execute_operation")  # type: ignore[untyped-decorator]
def execute_operation(*, operation_id: str) -> ExecutionResult:
    """Run one stored operation through the trusted worker boundary.

    A halted live activation raises before the operation is touched, so
    rollback stops new job execution in already-running workers.
    """
    require_live_runtime(os.environ)
    return _execute(UUID(operation_id))


@shared_task(name="comms.dispatch_due_reminders")  # type: ignore[untyped-decorator]
def dispatch_due_reminders() -> int:
    """Recover committed outbox rows without long-lived broker ETA messages.

    Beat runs once per minute. Retry rows wait at least one minute; each row
    has a three-send ceiling. Duplicate dispatches share task 13's claim lock.
    An abandoned in-progress operation reconciles, never blindly resends.
    A halted live activation raises before any row is claimed or dispatched.
    """
    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.comms_due_reminders_v1()")
        operation_ids = [str(row[0]) for row in cursor.fetchall()]
    for operation_id in operation_ids:
        execute_operation.apply_async(kwargs={"operation_id": operation_id})
    return len(operation_ids)
