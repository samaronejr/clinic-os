"""Audited owner-connection provisioning; no runtime authority-table write grants.

Call with an existing owner tenant transaction, bound operator GUCs and the
owner CLI's authentication/step-up boundary. Never expose these as web handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import (
    CareTeamMembership,
    Clinic,
    ProfessionalRegistration,
    RoleGrant,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

_DENIED = "current actor unauthorized"


@dataclass(frozen=True, slots=True, kw_only=True)
class CareTeamAssignment:
    """Target user and patient scope, never the acting operator."""

    user_id: UUID
    patient_enrollment_id: UUID
    role: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfessionalIdentity:
    """Synthetic professional identity bound to a target user."""

    user_id: UUID
    role: str
    council: str
    number: str
    jurisdiction: str
    specialty: str
    status: str
    physician_profile_id: UUID | None = None


def _owner_clinic(clinic_id: UUID) -> Clinic:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("clinic_owner",):
            raise CurrentActorError(_DENIED)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
        require_permission("staff.organization", clinic_id=clinic_id)
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_owner")
    return Clinic.objects.get(pk=clinic_id)


@transaction.atomic
def narrow_role(
    *,
    clinic_id: UUID,
    role: str,
    permission: str,
    valid_from: datetime,
    valid_to: datetime | None = None,
) -> RoleGrant:
    """Append a clinic subtraction; database checks reject additions and widening."""
    clinic = _owner_clinic(clinic_id)
    grant = RoleGrant.objects.create(
        organization_id=clinic.organization_id,
        clinic=clinic,
        role=role,
        permission=permission,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    record_phase1_event(
        "identity.role_grant.created", clinic_id=clinic_id, affected_record_id=grant.pk
    )
    return grant


@transaction.atomic
def assign_care_team(
    *,
    clinic_id: UUID,
    assignment: CareTeamAssignment,
    valid_from: datetime,
    valid_to: datetime | None = None,
) -> CareTeamMembership:
    """Bind subject scope without creating or widening canonical membership."""
    clinic = _owner_clinic(clinic_id)
    membership = CareTeamMembership.objects.create(
        organization_id=clinic.organization_id,
        clinic=clinic,
        user_id=assignment.user_id,
        patient_enrollment_id=assignment.patient_enrollment_id,
        role=assignment.role,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    record_phase1_event(
        "identity.care_team.created",
        clinic_id=clinic_id,
        affected_record_id=membership.pk,
    )
    return membership


@transaction.atomic
def register_professional(
    *,
    clinic_id: UUID,
    identity: ProfessionalIdentity,
    valid_from: datetime,
    valid_to: datetime,
) -> ProfessionalRegistration:
    """Record explicitly synthetic council evidence, never live verification."""
    clinic = _owner_clinic(clinic_id)
    registration = ProfessionalRegistration.objects.create(
        organization_id=clinic.organization_id,
        clinic=clinic,
        user_id=identity.user_id,
        role=identity.role,
        council=identity.council,
        number=identity.number,
        jurisdiction=identity.jurisdiction,
        specialty=identity.specialty,
        status=identity.status,
        valid_from=valid_from,
        valid_to=valid_to,
        physician_profile_id=identity.physician_profile_id,
        synthetic=True,
    )
    record_phase1_event(
        "identity.professional_registration.created",
        clinic_id=clinic_id,
        affected_record_id=registration.pk,
    )
    return registration


@transaction.atomic
def revoke_care_team(*, clinic_id: UUID, membership_id: UUID) -> CareTeamMembership:
    """Revoke once; serialize retries and retain the complete authority history."""
    _owner_clinic(clinic_id)
    membership = (
        CareTeamMembership.objects.select_for_update()
        .filter(
            pk=membership_id,
            clinic_id=clinic_id,
        )
        .first()
    )
    if membership is None:
        raise CurrentActorError(_DENIED)
    if membership.revoked_at is None:
        membership.revoked_at = timezone.now()
        membership.save(update_fields=("revoked_at",))
        record_phase1_event(
            "identity.care_team.revoked",
            clinic_id=clinic_id,
            affected_record_id=membership.pk,
        )
    return membership


@transaction.atomic
def revoke_professional(
    *,
    clinic_id: UUID,
    registration_id: UUID,
) -> ProfessionalRegistration:
    """Revoke a synthetic registration without deleting professional history."""
    _owner_clinic(clinic_id)
    registration = (
        ProfessionalRegistration.objects.select_for_update()
        .filter(
            pk=registration_id,
            clinic_id=clinic_id,
        )
        .first()
    )
    if registration is None:
        raise CurrentActorError(_DENIED)
    if registration.revoked_at is None:
        registration.revoked_at = timezone.now()
        registration.save(update_fields=("revoked_at",))
        record_phase1_event(
            "identity.professional_registration.revoked",
            clinic_id=clinic_id,
            affected_record_id=registration.pk,
        )
    return registration
