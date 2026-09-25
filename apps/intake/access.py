"""Fail-closed clinic-manager authorization for intake services."""

from __future__ import annotations

from uuid import UUID

from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    require_current_actor_clinic_roles,
    require_permission,
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


def authorized_enrollment_for(
    clinic_id: UUID,
    enrollment_id: UUID,
    permission: str,
) -> tuple[UUID, Clinic, PatientClinicEnrollment]:
    """Resolve actor, clinic and enrollment under one exact permission.

    Unlike ``authorized_enrollment`` this binds the new permission-bundle
    authority (todo 6): the actor must hold ``permission`` for the clinic
    and the enrollment must belong to that clinic inside the current
    tenant; every failure answers identically.
    """
    if type(clinic_id) is not UUID or type(enrollment_id) is not UUID:
        raise PatientAccessDeniedError
    try:
        actor_id = require_permission(
            permission,
            clinic_id=clinic_id,
            patient_enrollment_id=enrollment_id,
        )
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
