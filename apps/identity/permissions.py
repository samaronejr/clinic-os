"""Immutable v1 staff bundles and the unchanged legacy DRF role boundary.

A bundle is eligibility, not a substitute for a domain's assignment, release,
class, step-up or approval checks. Restricted notes and non-staff identities
have no default staff grant. Never edit v1 after release: add a new version.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Final
from uuid import UUID

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.identity.models import UserClinicRole
from apps.identity.services import ClinicId, ClinicRoles, UserId, has_clinic_role

# Explicit action names prevent partial RP cells (read, route, propose) from
# becoming full clinical or financial authority.
_PHYSICIAN: Final = frozenset(
    {
        "appointment.read_own",
        "appointment.book_own",
        "appointment.move_own",
        "demographics.read",
        "demographics.write",
        "clinical.read",
        "clinical.write",
        "clinical.finalize",
        "clinical.amend",
        "order.place",
        "result.read",
        "result.acknowledge",
        "result.release",
        "prescription.prepare",
        "prescription.sign",
        "charge.read",
        "tiss.clinical_read",
        "configuration.propose",
        "automation.propose_clinical",
        "break_glass.request",
    }
)
_NURSE: Final = frozenset(
    {
        "appointment.read",
        "demographics.read",
        "observation.write",
        "order.observe",
        "order.task",
        "break_glass.request_scoped",
    }
)
_RECEPTION: Final = frozenset(
    {
        "appointment.read",
        "appointment.book",
        "appointment.move",
        "demographics.read",
        "demographics.write",
        "order.route",
        "charge.read",
        "charge.create",
        "charge.collect",
    }
)
_MANAGER: Final = frozenset(
    {
        "appointment.read",
        "appointment.book",
        "appointment.move",
        "demographics.read",
        "charge.read",
        "refund.request",
        "writeoff.request",
        "payout.request",
        "tiss.read",
        "configuration.clinic",
        "staff.clinic",
        "automation.admin",
    }
)
_FINANCE: Final = frozenset(
    {
        "appointment.read",
        "demographics.billing_read",
        "charge.read",
        "charge.create",
        "charge.collect",
        "settlement.post",
        "refund.approve",
        "writeoff.approve",
        "payout.approve",
        "tiss.read",
        "tiss.manage",
        "configuration.clinic",
        "automation.finance",
    }
)
_ORG_ADMIN: Final = frozenset(
    {
        "appointment.read",
        "charge.read",
        "finance.policy",
        "tiss.read",
        "configuration.organization",
        "staff.organization",
        "automation.organization",
    }
)
BUNDLES_V1: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "physician": _PHYSICIAN,
        "nurse": _NURSE,
        "allied_professional": _NURSE,
        "receptionist": _RECEPTION,
        "scheduler": _RECEPTION,
        "clinic_manager": _MANAGER,
        "clinic_admin": _MANAGER,
        "finance": _FINANCE,
        "org_admin": _ORG_ADMIN,
        "owner": _ORG_ADMIN,
    }
)
PERMISSIONS: Final[frozenset[str]] = frozenset().union(
    *BUNDLES_V1.values(), {"restricted.read"}
)
# Patient-specific clinical actions require a current professional registration
# and assigned/care-team scope. Break-glass here only requests a future grant.
PROFESSIONAL_PERMISSIONS_V1: Final = frozenset(
    {
        "clinical.read",
        "clinical.write",
        "clinical.finalize",
        "clinical.amend",
        "observation.write",
        "order.place",
        "result.read",
        "result.acknowledge",
        "result.release",
        "order.observe",
        "order.task",
        "prescription.prepare",
        "prescription.sign",
        "tiss.clinical_read",
        "break_glass.request",
        "break_glass.request_scoped",
    }
)
PATIENT_SCOPED_PERMISSIONS_V1: Final = PROFESSIONAL_PERMISSIONS_V1 - {
    "break_glass.request",
    "break_glass.request_scoped",
}


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
