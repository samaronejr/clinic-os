"""Shared scope and retained links for explicit domain-owned forward handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from django.db import connection

from apps.audit.services import record_phase1_event
from apps.core.idempotency import create_fingerprint
from apps.intake.access import PatientAccessDeniedError
from apps.intake.alias_access import review_actor
from apps.intake.models import (
    MergeDecision,
    PatientClinicEnrollment,
    PatientMergeTransfer,
)

if TYPE_CHECKING:
    from apps.identity.models import Clinic


@dataclass(frozen=True, slots=True)
class MergeTransferScope:
    """Actual stored adjudication and explicit source/survivor clinic enrollments."""

    clinic: Clinic
    decision: MergeDecision
    original: PatientClinicEnrollment
    survivor: PatientClinicEnrollment


@dataclass(frozen=True, slots=True)
class ForwardLink:
    """One domain handler's actual old/new stored-row identities."""

    kind: Literal["appointment", "waitlist", "contact", "preference", "access_grant"]
    original_id: UUID | int
    replacement_id: UUID | int
    action: Literal["recreated", "kept_survivor", "revoked_and_reissued"] = "recreated"


def merge_transfer_scope(
    *, clinic_id: UUID, merge_decision_id: UUID
) -> MergeTransferScope:
    """Require the current transaction's persisted, exact-session adjudication."""
    _actor, clinic = review_actor(clinic_id)
    decision = MergeDecision.objects.filter(pk=merge_decision_id).first()
    if decision is None:
        raise PatientAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.intake_merge_mutation_allowed(%s,%s) "
            "AND current_setting('app.current_merge_decision',true)=%s",
            [clinic_id, decision.merged_patient_id, str(merge_decision_id)],
        )
        if cursor.fetchone() != (True,):
            raise PatientAccessDeniedError
    original = (
        PatientClinicEnrollment.objects.select_related("patient")
        .filter(clinic_id=clinic_id, patient_id=decision.merged_patient_id)
        .first()
    )
    if original is None:
        raise PatientAccessDeniedError
    survivor = (
        PatientClinicEnrollment.objects.select_related("patient")
        .filter(clinic_id=clinic_id, patient_id=decision.survivor_patient_id)
        .first()
    )
    if survivor is None:
        from apps.intake.models import Patient  # noqa: PLC0415

        patient = Patient.objects.get(pk=decision.survivor_patient_id)
        survivor = PatientClinicEnrollment.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            patient=patient,
            idempotency_key=uuid5(decision.pk, f"enrollment:{clinic_id}"),
            create_fingerprint=create_fingerprint(
                "patient",
                {
                    "birth_date": patient.birth_date.isoformat()
                    if patient.birth_date
                    else "",
                    "clinic_id": str(clinic_id),
                    "full_name": patient.full_name,
                },
            ),
        )
    return MergeTransferScope(clinic, decision, original, survivor)


def record_forward_link(scope: MergeTransferScope, link: ForwardLink) -> None:
    """Retain the actual immutable link under the current human adjudicator."""
    receipt = PatientMergeTransfer.objects.create(
        organization_id=scope.clinic.organization_id,
        clinic=scope.clinic,
        merge_decision=scope.decision,
        kind=link.kind,
        original_id=str(link.original_id),
        replacement_id=str(link.replacement_id),
        action=link.action,
    )
    record_phase1_event(
        "intake.duplicate.forward_transferred",
        clinic_id=scope.clinic.pk,
        affected_record_id=receipt.pk,
    )
