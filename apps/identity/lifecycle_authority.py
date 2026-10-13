"""Human, exact-clinic authority shared by staff lifecycle services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.core.exceptions import PermissionDenied
from django.db import connection

from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.current_context import UserId

MANAGEMENT_COLUMNS: Final = 2


class LifecycleDeniedError(PermissionDenied):
    """Refuse missing, foreign or narrowed authority without reflecting selectors."""


def require_staff_management(*, clinic_id: UUID) -> tuple[UserId, Clinic]:
    """Authorize the current human manager/admin within the caller transaction.

    Both alternatives use actual canonical SQL verdicts. None of a role name,
    registry declaration, principal grant or request claim substitutes for this
    exact-clinic decision, and malformed results cannot become a truthy grant.
    """
    if not connection.in_atomic_block:
        raise LifecycleDeniedError
    try:
        actor_id = current_actor_id()
    except CurrentActorError as error:
        raise LifecycleDeniedError from error
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT clinic_app.has_permission('staff.clinic', %s, NULL),
                   clinic_app.has_permission('staff.organization', %s, NULL)
            """,
            [clinic_id, clinic_id],
        )
        row = cursor.fetchone()
    if (
        row is None
        or len(row) != MANAGEMENT_COLUMNS
        or type(row[0]) is not bool
        or type(row[1]) is not bool
        or row == (False, False)
    ):
        raise LifecycleDeniedError
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None:
        raise LifecycleDeniedError
    return actor_id, clinic
