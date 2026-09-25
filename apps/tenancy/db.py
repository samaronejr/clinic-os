"""Transaction-scoped PostgreSQL tenant context."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from django.db import connection, connections, transaction


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


def clear_connection_tenant_gucs() -> None:
    """Reset persistent tenant state without opening an unused connection."""
    if connection.connection is None:
        return

    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute("RESET app.current_user_id")
        cursor.execute("RESET app.current_tenant")
        cursor.execute("RESET app.current_patient_session")
        cursor.execute("RESET app.current_principal")


@contextmanager
def tenant_context(user_id: UUID, org_id: UUID) -> Iterator[None]:
    """Authorize and expose one tenant only for one outermost transaction."""
    if connection.in_atomic_block or (
        "agent" in connections and connections["agent"].in_atomic_block
    ):
        raise TenantTransactionNestingError

    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_user_id', %s, true) "
                    "WHERE current_user IN ('clinic_app', 'clinic_owner')",
                    [str(user_id)],
                )
                if cursor.fetchone() != (str(user_id),):
                    raise TenantAccessDeniedError(
                        user_id=user_id, organization_id=org_id
                    )
                cursor.execute(
                    "SELECT clinic_app.user_has_org(%s)",
                    [str(org_id)],
                )
                membership = cursor.fetchone()
                if membership != (True,):
                    raise TenantAccessDeniedError(
                        user_id=user_id,
                        organization_id=org_id,
                    )
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [str(org_id)],
                )
            yield
    finally:
        clear_connection_tenant_gucs()


class ServicePrincipalAccessDeniedError(TenantContextError):
    """Deny unknown, mismatched, ungranted and revoked machine identities alike."""

    def __init__(self) -> None:
        """Keep credential and tenant selectors out of error messages."""
        super().__init__("service principal access denied")


@contextmanager
def service_principal_context(*, principal_id: UUID, clinic_id: UUID) -> Iterator[None]:
    """Own one grant-checked transaction on the dedicated ``agent`` alias.

    Callers explicitly use ``.using('agent')``. No staff connection or actor
    is borrowed. The resolver authenticates session_user, the registered
    clinic and a current grant before any tenant/principal GUC is installed.
    RLS repeats that check on every statement, including after revocation.
    """
    if "agent" not in connections:
        raise ServicePrincipalAccessDeniedError
    agent = connections["agent"]
    if connection.in_atomic_block or agent.in_atomic_block:
        raise TenantTransactionNestingError
    try:
        if not isinstance(principal_id, UUID) or not isinstance(clinic_id, UUID):
            raise ServicePrincipalAccessDeniedError
        with transaction.atomic(using="agent", durable=True):
            with agent.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.principal_scope(%s, %s)",
                    [principal_id, clinic_id],
                )
                row = cursor.fetchone()
                if row is None or not isinstance(row[0], UUID):
                    raise ServicePrincipalAccessDeniedError
                cursor.execute(
                    "SELECT set_config('app.current_principal', %s, true), "
                    "set_config('app.current_tenant', %s, true)",
                    [str(principal_id), str(row[0])],
                )
            yield
    finally:
        if agent.connection is not None:
            with (
                transaction.atomic(using="agent", durable=True),
                agent.cursor() as cursor,
            ):
                cursor.execute("RESET app.current_principal")
                cursor.execute("RESET app.current_tenant")
                cursor.execute("RESET app.current_user_id")
                cursor.execute("RESET app.current_patient_session")
