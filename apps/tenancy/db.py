"""Transaction-scoped PostgreSQL tenant context."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from django.db import connection, transaction


class TenantContextError(Exception):
    """Base error for rejected tenant context boundaries."""


class TenantAccessDeniedError(TenantContextError):
    """Report a user and organization pair without membership."""

    user_id: UUID
    organization_id: UUID

    def __init__(self, user_id: UUID, organization_id: UUID) -> None:
        """Store typed identifiers while exposing a non-identifying message."""
        super().__init__("tenant access denied")
        self.user_id = user_id
        self.organization_id = organization_id


class TenantTransactionNestingError(TenantContextError):
    """Reject a context that cannot own the complete transaction lifetime."""

    def __init__(self) -> None:
        """Expose the outermost-transaction requirement."""
        super().__init__("tenant context requires an outermost transaction")


@contextmanager
def tenant_context(user_id: UUID, org_id: UUID) -> Iterator[None]:
    """Authorize and expose one tenant only for one outermost transaction."""
    if connection.in_atomic_block:
        raise TenantTransactionNestingError

    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(user_id)],
            )
            cursor.execute(
                "SELECT clinic_app.user_has_org(%s)",
                [str(org_id)],
            )
            membership = cursor.fetchone()
            if membership != (True,):
                raise TenantAccessDeniedError(user_id=user_id, organization_id=org_id)
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(org_id)],
            )
        yield
