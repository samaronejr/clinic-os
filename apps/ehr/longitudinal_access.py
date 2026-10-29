"""Patient scope is re-decided from current professional and care permissions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PatientScope:
    """Validated stored scope and current actor; never caller-supplied authority."""

    enrollment_id: UUID
    organization_id: UUID
    clinic_id: UUID
    patient_id: UUID
    user_id: UUID


def patient_scope(
    *, clinic_id: UUID, enrollment_id: UUID, action: Literal["read", "observe", "write"]
) -> PatientScope:
    """Nursing observation scope cannot become narrative or medication scope."""
    enrollment = PatientClinicEnrollment.objects.filter(
        pk=enrollment_id, clinic_id=clinic_id
    ).first()
    if enrollment is None:
        raise ClinicalAccessDeniedError
    permissions: tuple[str, ...]
    match action:
        case "read":
            permissions = ("clinical.read", "observation.write")
        case "observe":
            permissions = ("clinical.write", "observation.write")
        case "write":
            permissions = ("clinical.write",)
    for permission in permissions:
        try:
            actor = require_permission(
                permission,
                clinic_id=clinic_id,
                patient_enrollment_id=enrollment_id,
            )
        except CurrentActorError:
            continue
        return PatientScope(
            enrollment_id,
            enrollment.organization_id,
            clinic_id,
            enrollment.patient_id,
            actor,
        )
    raise ClinicalAccessDeniedError
