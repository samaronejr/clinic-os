"""Fail-closed clinic-manager authorization for intake services."""

from __future__ import annotations

from uuid import UUID

from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic
from apps.intake.models import PatientClinicEnrollment


class PatientAccessDeniedError(Exception):
    """Hide whether actor, clinic assignment, or tenant scope was rejected."""

    def __init__(self) -> None:
        """Expose one stable non-identifying denial message."""
        super().__init__("patient access denied")


def authorized_manager_clinic(clinic_id: UUID) -> Clinic:
    """Load one clinic only after current-actor manager authorization."""
    try:
        require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
        return Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise PatientAccessDeniedError from error


def authorized_enrollment(
    clinic_id: UUID,
    enrollment_id: UUID,
) -> tuple[UUID, Clinic, PatientClinicEnrollment]:
    """Resolve actor, clinic and enrollment under manager authorization."""
    if type(clinic_id) is not UUID or type(enrollment_id) is not UUID:
        raise PatientAccessDeniedError
    try:
        actor_id = require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
        clinic = Clinic.objects.get(pk=clinic_id)
        enrollment = PatientClinicEnrollment.objects.select_related("patient").get(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            pk=enrollment_id,
        )
    except (
        CurrentActorError,
        Clinic.DoesNotExist,
        PatientClinicEnrollment.DoesNotExist,
    ) as error:
        raise PatientAccessDeniedError from error
    return UUID(str(actor_id)), clinic, enrollment
