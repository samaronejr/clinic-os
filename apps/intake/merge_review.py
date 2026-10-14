"""Minimum-necessary review of retained forward links and later survivor rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from apps.billing.models import Invoice
from apps.ehr.models import Encounter
from apps.intake.access import PatientAccessDeniedError
from apps.intake.alias_access import review_actor
from apps.intake.models import MergeDecision, PatientMergeTransfer
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class MergeReview:
    """Visible metadata that stays on the survivor after an unmerge."""

    decision: MergeDecision
    transfers: tuple[PatientMergeTransfer, ...]
    later_appointments: tuple[UUID, ...]
    later_encounters: tuple[UUID, ...]
    later_invoices: tuple[UUID, ...]


def merge_review(*, clinic_id: UUID, merge_decision_id: UUID) -> MergeReview:
    """List later rows through their unchanged domain RLS, never move them."""
    with transaction.atomic():
        review_actor(clinic_id)
        decision = MergeDecision.objects.filter(
            pk=merge_decision_id, clinic_id=clinic_id
        ).first()
        if decision is None:
            raise PatientAccessDeniedError
        scope = {
            "clinic_id": clinic_id,
            "patient_id": decision.survivor_patient_id,
            "created_at__gte": decision.created_at,
        }
        return MergeReview(
            decision=decision,
            transfers=tuple(
                PatientMergeTransfer.objects.filter(
                    merge_decision=decision, clinic_id=clinic_id
                ).order_by("kind", "original_id")
            ),
            later_appointments=tuple(
                Appointment.objects.filter(**scope)
                .order_by("id")
                .values_list("id", flat=True)
            ),
            later_encounters=tuple(
                Encounter.objects.filter(**scope)
                .order_by("id")
                .values_list("id", flat=True)
            ),
            later_invoices=tuple(
                Invoice.objects.filter(**scope)
                .order_by("id")
                .values_list("id", flat=True)
            ),
        )
