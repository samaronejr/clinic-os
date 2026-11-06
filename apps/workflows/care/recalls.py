"""Recalls: a scheduled return prompt that becomes one human-owned task when due."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import current_actor_id
from apps.identity.models import UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.workflows import engine
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.access import keyed_digests, require_staff, save_command
from apps.workflows.care.models import Recall
from apps.workflows.care.validation import due_after_days
from apps.workflows.care.vocab import RECALL_REASONS
from apps.workflows.task_services import assign_task, create_task
from apps.workflows.validation import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    digest,
)

if TYPE_CHECKING:
    from datetime import datetime


@transaction.atomic
def schedule_recall(  # noqa: PLR0913 - pinned keyword contract
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    reason: str,
    interval_days: int,
    owner_role: str,
    idempotency_key: UUID,
) -> Recall:
    """Schedule one recall ``interval_days`` after the injected clock's now."""
    now: datetime = engine.utc_now()
    due_at = due_after_days(now=now, days=interval_days)
    if (
        reason not in RECALL_REASONS
        or owner_role not in UserClinicRole.Role.values
        or not isinstance(idempotency_key, UUID)
        or not isinstance(enrollment_id, UUID)
    ):
        raise WorkflowInputError
    clinic = require_staff(clinic_id=clinic_id, permission="tasks.reassign")
    if not PatientClinicEnrollment.objects.filter(
        pk=enrollment_id, clinic_id=clinic_id
    ).exists():
        raise WorkflowAccessDeniedError
    actor = current_actor_id()
    fingerprints = keyed_digests(
        {
            "actor": str(actor),
            "enrollment": str(enrollment_id),
            "reason": reason,
            "interval_days": interval_days,
            "owner_role": owner_role,
        }
    )
    replay = Recall.objects.filter(
        clinic_id=clinic_id, idempotency_key=idempotency_key
    ).first()
    if replay is not None:
        if replay.fingerprint not in fingerprints:
            raise WorkflowConflictError
        return replay
    recall = Recall.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        reason=reason,
        interval_days=interval_days,
        anchor_at=now,
        due_at=due_at,
        owner_role=owner_role,
        created_by_id=actor,
        idempotency_key=idempotency_key,
        fingerprint=fingerprints[-1],
    )
    record_phase1_event(
        "workflows.recall.scheduled", clinic_id=clinic_id, affected_record_id=recall.pk
    )
    return recall


@transaction.atomic
def materialize_recall(*, clinic_id: UUID, recall_id: UUID) -> bool:
    """Turn one due recall into its role-owned follow-up task, once.

    Returns False when the recall is no longer due (materialized or early).
    """
    require_staff(clinic_id=clinic_id, permission="tasks.reassign")
    recall = (
        Recall.objects.select_for_update()
        .filter(pk=recall_id, clinic_id=clinic_id)
        .first()
    )
    if recall is None:
        raise WorkflowAccessDeniedError
    if recall.state != "scheduled" or recall.due_at > engine.utc_now():
        return False
    task = create_task(
        clinic_id=clinic_id,
        spec=TaskSpec(
            kind="follow_up",
            subject_ref={"kind": "enrollment", "id": str(recall.enrollment_id)},
            due_at=recall.due_at,
        ),
        idempotency_key=recall.pk,
    )
    assigned = assign_task(
        clinic_id=clinic_id,
        task_id=task.pk,
        owner=TaskOwner(role=recall.owner_role),
        expected_revision=task.revision,
    )
    recall.task_id = assigned.pk
    recall.state = "materialized"
    recall.last_command_key = uuid5(recall.pk, "materialize")
    recall.last_command_digest = digest({"recall": str(recall.pk)})
    save_command(recall, ("state", "task_id"), "workflows.recall.materialized")
    return True
