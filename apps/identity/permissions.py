"""DRF permissions backed only by canonical clinic role assignments."""

from typing import ClassVar
from uuid import UUID

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.identity.models import UserClinicRole
from apps.identity.services import ClinicId, ClinicRoles, UserId, has_clinic_role


def _parse_uuid(raw_value: str | UUID | None) -> UUID | None:
    if raw_value is None:
        return None
    try:
        return UUID(str(raw_value))
    except ValueError:
        return None


class ClinicRolePermission(BasePermission):
    """Require an assignment for the clinic identifier in the current route."""

    required_roles: ClassVar[ClinicRoles] = ()
    clinic_url_kwarg: ClassVar[str] = "clinic_id"

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Fail closed for anonymous, malformed, or unassigned requests."""
        if not request.user.is_authenticated:
            return False
        user_id = _parse_uuid(request.user.pk)
        clinic_id = _parse_uuid(view.kwargs.get(self.clinic_url_kwarg))
        if user_id is None or clinic_id is None:
            return False
        return has_clinic_role(
            UserId(user_id),
            ClinicId(clinic_id),
            self.required_roles,
        )


class IsPhysicianForClinic(ClinicRolePermission):
    """Allow only a physician assigned to the requested clinic."""

    required_roles = (UserClinicRole.Role.PHYSICIAN,)


class IsClinicAdminForClinic(ClinicRolePermission):
    """Allow the clinic administrator or organization owner assignment."""

    required_roles = (
        UserClinicRole.Role.OWNER,
        UserClinicRole.Role.CLINIC_ADMIN,
    )
