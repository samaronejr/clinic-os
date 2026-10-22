"""Read-committed permission checks, opaque selectors and version fencing."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.db import connection

from apps.identity.current_context import CurrentActorError, require_permission
from apps.orders.models import ResultMatch, ResultVersion
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError

if TYPE_CHECKING:
    from uuid import UUID


def require_ingest(*, clinic_id: UUID, order_id: UUID | None) -> UUID | None:
    """Return staff identity or None for the separately authenticated lab principal."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.orders_ingest_allowed(%s,%s), "
            "NULLIF(current_setting('app.current_user_id',true),'')::uuid",
            [clinic_id, order_id],
        )
        row = cursor.fetchone()
    if row is None or row[0] is not True:
        raise OrdersAccessDeniedError
    return cast("UUID | None", row[1])


def require_version(
    *, clinic_id: UUID, version_id: UUID, permission: str
) -> ResultMatch:
    """Require exact matching and the current source for a clinical action."""
    matched = (
        ResultMatch.objects.select_related("order")
        .filter(version_id=version_id, clinic_id=clinic_id)
        .first()
    )
    if matched is None:
        raise OrdersAccessDeniedError
    try:
        require_permission(
            permission,
            clinic_id=clinic_id,
            patient_enrollment_id=matched.order.enrollment_id,
        )
    except CurrentActorError as error:
        raise OrdersAccessDeniedError from error
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.orders_is_current(%s)", [version_id])
        if cursor.fetchone() != (True,):
            raise OrdersConflictError(code="source_superseded")
    return matched


def lock_series(*, identifier: UUID) -> None:
    """Serialize replay, correction and clinical decisions on the same series."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"orders-series:{identifier}"],
        )


def current_version(*, clinic_id: UUID, version_id: UUID) -> ResultVersion:
    """Find the exact source without granting any clinical permission."""
    row = ResultVersion.objects.filter(pk=version_id, clinic_id=clinic_id).first()
    if row is None:
        raise OrdersAccessDeniedError
    lock_series(identifier=row.result_id)
    return row
