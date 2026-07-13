"""Tenant-bound identity role and login organization services."""

from typing import ClassVar, NewType
from uuid import UUID

from django.db import connection, transaction
from django.db.models import QuerySet
from django.dispatch import Signal
from django.http import HttpRequest

from apps.identity.models import Clinic, User, UserClinicRole

UserId = NewType("UserId", UUID)
ClinicId = NewType("ClinicId", UUID)
OrganizationId = NewType("OrganizationId", UUID)

type ClinicRoles = tuple[UserClinicRole.Role, ...]


def role_assignments_for_user(user_id: UserId) -> QuerySet[UserClinicRole]:
    """Return assignments visible for one user in the active tenant."""
    return UserClinicRole.objects.filter(user_id=user_id)


def clinics_for_user_roles(
    user_id: UserId,
    roles: ClinicRoles,
) -> QuerySet[Clinic]:
    """Return distinct active-tenant clinics granted by canonical assignments."""
    if not roles:
        return Clinic.objects.none()
    return (
        Clinic.objects.filter(
            userclinicrole__user_id=user_id,
            userclinicrole__role__in=roles,
        )
        .distinct()
        .order_by("pk")
    )


def has_clinic_role(
    user_id: UserId,
    clinic_id: ClinicId,
    roles: ClinicRoles,
) -> bool:
    """Check one clinic assignment without crossing the active tenant boundary."""
    if not roles:
        return False
    return (
        role_assignments_for_user(user_id)
        .filter(
            clinic_id=clinic_id,
            role__in=roles,
        )
        .exists()
    )


class ClinicRoleQuerySetMixin:
    """Expose a reusable role-scoped clinic queryset for tenant views."""

    required_roles: ClassVar[ClinicRoles] = ()

    def clinics_for_user(self, user_id: UserId) -> QuerySet[Clinic]:
        """Apply the view's role contract to the canonical assignments."""
        return clinics_for_user_roles(user_id, self.required_roles)


def resolve_login_organization(user_id: UserId) -> OrganizationId | None:
    """Choose the lowest membership organization through the hardened resolver."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(user_id)],
        )
        cursor.execute(
            """
            SELECT organization_id
            FROM clinic_app.user_organizations()
                AS organizations(organization_id)
            ORDER BY organization_id
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return OrganizationId(UUID(str(row[0])))


def set_active_organization_on_login(
    sender: type[User],
    request: HttpRequest,
    user: User,
    **_signal_context: Signal,
) -> None:
    """Replace caller state with the authoritative deterministic membership."""
    del sender, _signal_context
    organization_id = resolve_login_organization(UserId(user.pk))
    if organization_id is None:
        request.session.pop("active_org_id", None)
        return
    request.session["active_org_id"] = str(organization_id)
