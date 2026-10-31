"""Durable minute scanner for care records; messages carry no authority.

Each due record becomes its own job, run as the record's stored actor with
current authority rechecked. A record that actor cannot process (refused,
revoked, no longer allowed to see it) is recorded in ``workflows_carerefusal``
and listed again only after ``vocab.SCAN_RETRY_MINUTES``: never swallowed, never
re-listed every minute, never able to starve other records. The listing takes
at most ``vocab.SCAN_PER_TENANT`` records per organization, oldest first.
"""

from __future__ import annotations

import os
from uuid import UUID

from celery import shared_task
from django.db import connection
from ops.release.activation import require_live_runtime

from apps.identity.current_context import CurrentActorError
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.workflows import engine
from apps.workflows.care.plans import flag_stale_care_plan, mark_care_action_overdue
from apps.workflows.care.pro import close_missed_pro_occurrence
from apps.workflows.care.recalls import materialize_recall
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError


def process_due_care(*, clinic_id: UUID, kind: str, record_id: UUID) -> bool:
    """Run one due record as the current actor; False when it is not due."""
    if kind == "recall":
        return materialize_recall(clinic_id=clinic_id, recall_id=record_id)
    if kind == "overdue":
        return mark_care_action_overdue(clinic_id=clinic_id, action_id=record_id)
    if kind == "pro_missed":
        return close_missed_pro_occurrence(clinic_id=clinic_id, occurrence_id=record_id)
    if kind == "stale":
        return flag_stale_care_plan(clinic_id=clinic_id, plan_id=record_id)
    raise WorkflowInputError


def run_care(*, kind: str, record_id: str) -> bool:
    """Run one due record as its stored actor; a refusal is recorded, not dropped."""
    require_live_runtime(os.environ)
    record = UUID(record_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT organization_id,clinic_id,actor_id "
            "FROM clinic_app.workflows_care_scope(%s,%s)",
            [kind, record],
        )
        scope = cursor.fetchone()
    if scope is None:
        return False
    try:
        with tenant_context(scope[2], scope[0]):
            if process_due_care(clinic_id=scope[1], kind=kind, record_id=record):
                return True
    except (CurrentActorError, TenantAccessDeniedError, WorkflowConflictError):
        pass
    # Still due after its actor's attempt: park it with the refusal evidence.
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.workflows_care_park(%s,%s)", [kind, record])
    return False


run_care_job = shared_task(name="apps.workflows.tasks.care_run")(run_care)


def scan_care() -> int:
    """Publish one opaque job per due record; each job reloads and rechecks."""
    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT kind,record_id FROM clinic_app.workflows_care_due(%s)",
            [engine.utc_now()],
        )
        rows = cursor.fetchall()
    for kind, record in rows:
        run_care_job.apply_async(kwargs={"kind": kind, "record_id": str(record)})
    return len(rows)


scan_care_job = shared_task(name="apps.workflows.tasks.care_scan")(scan_care)
