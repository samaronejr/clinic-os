"""Trusted database-bound current actor helpers."""

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from django.db import connection

from apps.identity.models import Clinic, User, UserClinicRole

UserId = NewType("UserId", UUID)
ClinicId = NewType("ClinicId", UUID)
type ClinicRoles = tuple[UserClinicRole.Role, ...]
_CATALOG_COLUMN_COUNT = 2
MANAGER_ROLES: ClinicRoles = (
    UserClinicRole.Role.CLINIC_ADMIN,
    UserClinicRole.Role.OWNER,
    UserClinicRole.Role.RECEPTIONIST,
)


@dataclass(frozen=True, slots=True)
class PhysicianCatalogEntry:
    """Typed physician catalog row exposed to scheduling callers."""

    user_id: UserId
    display_label: str


class CurrentActorError(Exception):
    """Reject unavailable or unauthorized current actor context."""


class _UnavailableActorError(CurrentActorError):
    def __init__(self) -> None:
        super().__init__("current actor unavailable")


class _UnauthorizedActorError(CurrentActorError):
    def __init__(self) -> None:
        super().__init__("current actor unauthorized")


class _InvalidCatalogError(CurrentActorError):
    def __init__(self) -> None:
        super().__init__("physician catalog returned invalid data")


class _InvalidUsernameError(CurrentActorError):
    def __init__(self) -> None:
        super().__init__("current actor has an invalid username")


def _actor_uuid_from_guc() -> UserId:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.current_setting('app.current_user_id', true)")
        row = cursor.fetchone()
    if row is None or not isinstance(row, tuple) or len(row) != 1:
        raise _UnavailableActorError
    value = row[0]
    if not isinstance(value, str) or not value:
        raise _UnavailableActorError
    try:
        return UserId(UUID(value))
    except ValueError as error:
        raise _UnavailableActorError from error


def _load_current_actor() -> User:
    expected_id = _actor_uuid_from_guc()
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.load_current_user()")
        row = cursor.fetchone()
        description = cursor.description
    if row is None or description is None:
        raise _UnavailableActorError
    field_names = [column.name for column in description]
    user = User.from_db(connection.alias, field_names, row)
    if not isinstance(user.pk, UUID) or user.pk != expected_id or not user.is_active:
        raise _UnavailableActorError
    return user


def current_actor_id() -> UserId:
    """Return the active actor bound to the transaction-local user GUC."""
    actor = _load_current_actor()
    if not isinstance(actor.pk, UUID):
        raise _UnavailableActorError
    return UserId(actor.pk)


def current_actor_username() -> str:
    """Return the current actor's username for display-bound records."""
    actor = _load_current_actor()
    username = actor.username
    if not isinstance(username, str) or not username:
        raise _InvalidUsernameError
    return username


def require_current_actor_clinic_roles(
    clinic_id: UUID,
    roles: ClinicRoles,
) -> UserId:
    """Require the current actor to hold one allowed exact clinic role."""
    actor_id = current_actor_id()
    if (
        not roles
        or not UserClinicRole.objects.filter(
            user_id=actor_id,
            clinic_id=clinic_id,
            role__in=roles,
        ).exists()
    ):
        raise _UnauthorizedActorError
    return actor_id


def require_current_actor_org_admin(
    organization_id: UUID,
    roles: ClinicRoles,
) -> UserId:
    """Require the current actor to hold an allowed role in every clinic.

    Organization-scoped settings (queue quotas) need authority that no
    single clinic's admin can satisfy: until a dedicated org_admin role
    exists, the equivalent existing authority is an allowed role on every
    clinic of the organization. An organization with no clinics has no
    satisfiable authority and fails closed.
    """
    actor_id = current_actor_id()
    clinic_ids = set(
        Clinic.objects.filter(organization_id=organization_id).values_list(
            "pk", flat=True
        )
    )
    covered = set(
        UserClinicRole.objects.filter(
            user_id=actor_id,
            organization_id=organization_id,
            role__in=roles,
        ).values_list("clinic_id", flat=True)
    )
    if not roles or not clinic_ids or not clinic_ids <= covered:
        raise _UnauthorizedActorError
    return actor_id


def require_permission(
    permission: str,
    *,
    clinic_id: UUID,
    patient_enrollment_id: UUID | None = None,
) -> UserId:
    """Recheck exact-clinic eligibility in the authoritative database on every call.

    No request cache: role removal, narrowing and care-team revocation take
    effect at the next statement under the application's READ COMMITTED txn.
    Unknown permissions/selectors have the same payload-free denial.
    """
    if (
        not isinstance(permission, str)
        or not isinstance(clinic_id, UUID)
        or (
            patient_enrollment_id is not None
            and not isinstance(patient_enrollment_id, UUID)
        )
    ):
        raise _UnauthorizedActorError
    actor_id = current_actor_id()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.has_permission(%s, %s, %s)",
            [permission, clinic_id, patient_enrollment_id],
        )
        if cursor.fetchone() != (True,):
            raise _UnauthorizedActorError
    return actor_id


def _active_clinic_physicians(
    clinic_id: UUID,
) -> tuple[PhysicianCatalogEntry, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT user_id, display_label "
            "FROM clinic_app.list_active_clinic_physicians(%s::uuid)",
            [clinic_id],
        )
        rows = cursor.fetchall()

    physicians: list[PhysicianCatalogEntry] = []
    for row in rows:
        if (
            len(row) != _CATALOG_COLUMN_COUNT
            or not isinstance(row[0], UUID)
            or not isinstance(row[1], str)
            or not row[1]
        ):
            raise _InvalidCatalogError
        physicians.append(
            PhysicianCatalogEntry(
                user_id=UserId(row[0]),
                display_label=row[1],
            )
        )
    return tuple(physicians)


def list_active_clinic_physicians(
    clinic_id: UUID,
) -> tuple[PhysicianCatalogEntry, ...]:
    """Return active physicians visible to the current clinic manager."""
    require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
    return _active_clinic_physicians(clinic_id)


def practitioner_display_label(practitioner_id: UUID, clinic_id: UUID) -> str:
    """Resolve a practitioner through catalog, self, then UUID fallback."""
    actor = _load_current_actor()
    if actor.pk == practitioner_id:
        username = actor.username
        if not isinstance(username, str) or not username:
            raise _InvalidUsernameError
        return username

    if UserClinicRole.objects.filter(
        user_id=actor.pk,
        clinic_id=clinic_id,
        role__in=MANAGER_ROLES,
    ).exists():
        for physician in _active_clinic_physicians(clinic_id):
            if physician.user_id == practitioner_id:
                return physician.display_label
    return str(practitioner_id)
