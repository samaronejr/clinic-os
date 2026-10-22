"""One transaction applies the survivor version and explicit forward handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.intake.demographics import _apply_demographics
from apps.intake.models import PatientAlias, PatientClinicEnrollment
from apps.intake.patient_merge import transfer_patient_forward as transfer_intake
from apps.scheduling.patient_merge import (
    transfer_patient_forward as transfer_scheduling,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.intake.demographics import _DemographicsMutation
    from apps.intake.models import MergeDecision
    from apps.intake.patient_access import IssuedInvitation


def execute_alias_merge(
    decision: MergeDecision,
    mutation: _DemographicsMutation,
    clinic_ids: tuple[UUID, ...],
) -> tuple[IssuedInvitation, ...]:
    """Apply only the persisted current-transaction decision, preserving history."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_merge_decision',true)")
        row = cursor.fetchone()
        previous = row[0] or "" if row else ""
        cursor.execute(
            "SELECT set_config('app.current_merge_decision',%s,true)",
            [str(decision.pk)],
        )
    invitations: list[IssuedInvitation] = []
    try:
        with transaction.atomic():
            _apply_demographics(mutation)
            original_clinics = tuple(
                PatientClinicEnrollment.objects.filter(
                    patient_id=decision.merged_patient_id,
                    clinic_id__in=clinic_ids,
                )
                .order_by("clinic_id")
                .values_list("clinic_id", flat=True)
            )
            for clinic_id in original_clinics:
                transfer_scheduling(clinic_id=clinic_id, merge_decision_id=decision.pk)
                invitations.extend(
                    transfer_intake(clinic_id=clinic_id, merge_decision_id=decision.pk)
                )
            PatientAlias.objects.create(
                organization_id=decision.organization_id,
                merged_patient_id=decision.merged_patient_id,
                survivor_patient_id=decision.survivor_patient_id,
                merge_decision=decision,
            )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_merge_decision',%s,true)", [previous]
            )
    return tuple(invitations)
