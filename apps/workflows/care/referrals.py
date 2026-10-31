"""Referral coordination and the link to the todo-33 referral document.

Existing referral documents were linked once by migration 0007; runtime code
creates only planned referrals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.prescription.models import DocumentKind, PrescriptionDraft
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.care.access import (
    bind_command,
    require_clinician,
    require_staff,
    save_command,
)
from apps.workflows.care.models import CarePlan, Referral
from apps.workflows.care.vocab import REFERRAL_STATUSES
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from uuid import UUID

NEXT: dict[str, frozenset[str]] = {
    "requested": frozenset({"scheduled", "cancelled"}),
    "scheduled": frozenset({"completed", "cancelled"}),
    "documented": frozenset({"scheduled", "cancelled"}),
}


def _referral(clinic_id: UUID, referral_id: UUID) -> Referral:
    referral = (
        Referral.objects.select_for_update()
        .filter(pk=referral_id, clinic_id=clinic_id)
        .first()
    )
    if referral is None:
        raise WorkflowAccessDeniedError
    return referral


@transaction.atomic
def link_referral_document(
    *, clinic_id: UUID, referral_id: UUID, draft_id: UUID, expected_revision: int
) -> Referral:
    """Link the planned referral to the clinician's referral document, once."""
    require_staff(clinic_id=clinic_id, permission="tasks.view")
    referral = _referral(clinic_id, referral_id)
    plan = (
        CarePlan.objects.filter(pk=referral.plan_id, clinic_id=clinic_id).first()
        if referral.plan_id
        else None
    )
    if plan is None:
        raise WorkflowAccessDeniedError
    require_clinician(clinic_id=clinic_id, enrollment_id=plan.enrollment_id)
    draft = PrescriptionDraft.objects.filter(pk=draft_id, clinic_id=clinic_id).first()
    if draft is None:
        raise WorkflowAccessDeniedError
    if not bind_command(
        referral, expected_revision, "link", {"document": str(draft.pk)}
    ):
        return referral
    if (
        referral.document_id is not None
        or draft.kind != DocumentKind.REFERRAL
        or draft.encounter_id != plan.encounter_id
    ):
        raise WorkflowConflictError
    referral.document_id = draft.pk
    save_command(referral, ("document_id",), "workflows.referral.linked")
    return referral


@transaction.atomic
def advance_referral(
    *, clinic_id: UUID, referral_id: UUID, status: str, expected_revision: int
) -> Referral:
    """Move coordination forward; staff execute it, nothing is sent from here."""
    if status not in REFERRAL_STATUSES:
        raise WorkflowInputError
    require_staff(clinic_id=clinic_id, permission="tasks.assign")
    referral = _referral(clinic_id, referral_id)
    if not bind_command(referral, expected_revision, "advance", {"status": status}):
        return referral
    if status not in NEXT.get(referral.status, frozenset()):
        raise WorkflowConflictError
    referral.status = status
    save_command(referral, ("status",), "workflows.referral.advanced")
    return referral
