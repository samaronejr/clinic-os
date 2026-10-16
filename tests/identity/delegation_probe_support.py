"""Shared values and real SQL observation for executable root delegations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.identity.current_context import _UnauthorizedActorError
from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from uuid import UUID

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class Seeded:
    """A real callable, its observable state and its accepted return value."""

    call: Callable[[], object]
    state: Callable[[], object]
    returns: Callable[[UUID], object] | None = None
    denial: type[Exception] = _UnauthorizedActorError


@dataclass(frozen=True)
class Case:
    """A single-permission adapter or an exhaustive multi-branch decision test."""

    seed: Callable[[RbacGraph, UUID], Seeded]
    database_role: str = "clinic_owner"
    permission: str = "staff.organization"
    session: bool = False
    # Predicates and surfaces with legacy/own fallbacks cannot be described as
    # one raising permission call. Their exercise checks every catalog role,
    # no membership and inactive membership on the real callable and SQL roots.
    exercise: Callable[[RbacGraph, str], None] | None = None
    permission_role: str = "owner"


@contextmanager
def checked_permissions() -> Iterator[list[str]]:
    """Record the permission names actually sent to the database authority."""
    asked: list[str] = []

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if "clinic_app.has_permission(" in sql and params:
            asked.append(str(params[0]))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        yield asked
