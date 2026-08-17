"""Fail-closed authorization shared by availability services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole

if TYPE_CHECKING:
    from uuid import UUID


class AvailabilityAccessDeniedError(Exception):
    """Hide whether actor, clinic assignment, or tenant scope was rejected."""

    def __init__(self) -> None:
        """Expose one stable non-identifying denial message."""
        super().__init__("availability access denied")


class AppointmentAccessDeniedError(Exception):
    """Hide whether appointment actor, clinic, or enrollment was rejected."""

    def __init__(self) -> None:
        """Expose one stable non-identifying denial message."""
        super().__init__("appointment access denied")


@dataclass(frozen=True, slots=True)
class AvailabilityViewScope:
    """Bind a visible clinic to all blocks or one physician UUID."""

    clinic: Clinic
    practitioner_id: UUID | None


def authorized_manager_clinic(clinic_id: UUID) -> Clinic:
    """Load one clinic only after current-actor manager authorization."""
    try:
        require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
        return Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise AvailabilityAccessDeniedError from error


def authorized_appointment_manager_clinic(clinic_id: UUID) -> Clinic:
    """Load one clinic only after current-actor booking authorization."""
    try:
        require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
        return Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise AppointmentAccessDeniedError from error


def authorized_view_scope(clinic_id: UUID) -> AvailabilityViewScope:
    """Authorize manager-all or physician-own availability visibility."""
    try:
        actor_id = current_actor_id()
        roles = set(
            UserClinicRole.objects.filter(
                clinic_id=clinic_id,
                user_id=actor_id,
            ).values_list("role", flat=True)
        )
        clinic = Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise AvailabilityAccessDeniedError from error
    if roles.intersection(MANAGER_ROLES):
        return AvailabilityViewScope(clinic=clinic, practitioner_id=None)
    if UserClinicRole.Role.PHYSICIAN in roles:
        return AvailabilityViewScope(clinic=clinic, practitioner_id=actor_id)
    raise AvailabilityAccessDeniedError
