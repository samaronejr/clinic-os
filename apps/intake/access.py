"""Fail-closed clinic-manager authorization for intake services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID


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
