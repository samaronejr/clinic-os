"""FORCE-RLS owner command context transitions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection

from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.management.base import LifecycleCommandError
from apps.identity.models import UserClinicRole

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

SAVED_CONTEXT_COLUMNS = 3
SUPPORTED_OWNER_ROLES = frozenset({"clinic_owner", "none"})


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    """Bind one operator to one organization and clinic."""

    operator_id: UUID
    organization_id: UUID
    clinic_id: UUID


@dataclass(frozen=True, slots=True)
class _SavedContext:
    role: str
    tenant: str | None
    user: str | None


def _saved_context() -> _SavedContext:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('role'), "
            "current_setting('app.current_tenant', true), "
            "current_setting('app.current_user_id', true)"
        )
        row = cursor.fetchone()
    if (
        row is None
        or len(row) != SAVED_CONTEXT_COLUMNS
        or not isinstance(row[0], str)
        or row[0] not in SUPPORTED_OWNER_ROLES
        or (row[1] is not None and not isinstance(row[1], str))
        or (row[2] is not None and not isinstance(row[2], str))
    ):
        raise LifecycleCommandError
    return _SavedContext(role=row[0], tenant=row[1], user=row[2])


def _set_gucs(organization_id: UUID, user_id: UUID | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(organization_id), "" if user_id is None else str(user_id)],
        )


def _restore_context(saved: _SavedContext) -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        if saved.role == "clinic_owner":
            cursor.execute("SET ROLE clinic_owner")
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [saved.tenant or "", saved.user or ""],
        )


@contextmanager
def scoped_lifecycle_gucs(
    organization_id: UUID,
    user_id: UUID | None,
) -> Iterator[None]:
    """Set lifecycle GUCs and restore the exact role and prior values."""
    if not connection.in_atomic_block:
        raise LifecycleCommandError
    saved = _saved_context()
    try:
        _set_gucs(organization_id, user_id)
        yield
    finally:
        if not connection.needs_rollback:
            _restore_context(saved)


@contextmanager
def scoped_owner_gucs(context: LifecycleContext) -> Iterator[None]:
    """Set and restore exact tenant/operator GUC values."""
    with scoped_lifecycle_gucs(context.organization_id, context.operator_id):
        yield


@contextmanager
def assume_runtime_owner(context: LifecycleContext) -> Iterator[None]:
    """Authorize the bound active owner only after assuming clinic_app."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
        actor_id = require_current_actor_clinic_roles(
            context.clinic_id,
            (UserClinicRole.Role.OWNER,),
        )
        if actor_id != context.operator_id:
            raise LifecycleCommandError
        yield
    except CurrentActorError as error:
        raise LifecycleCommandError from error
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
