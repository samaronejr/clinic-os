"""Execution custody: exact principal contexts and bounded serialized send ownership."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connections

from apps.ai.action_contracts import ActionAccessDeniedError
from apps.identity.current_context import current_actor_id
from apps.tenancy.db import service_principal_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID


@contextmanager
def action_scope(*, clinic_id: UUID, principal_id: UUID | None) -> Iterator[str]:
    """A1 stays in its human request; machine work owns its dedicated transaction."""
    if principal_id is None:
        if not connections["default"].in_atomic_block:
            raise ActionAccessDeniedError
        current_actor_id()
        yield "default"
    else:
        with service_principal_context(principal_id=principal_id, clinic_id=clinic_id):
            yield "agent"


@contextmanager
def send_custody(*, clinic_id: UUID, principal_id: UUID | None) -> Iterator[str]:
    """Hold the mutation/send lock across commit, dispatch and outcome persistence.

    The machine alias is a direct/session connection, never transaction pooled.
    Human A1 commands retain their transaction lock until the request commits.
    """
    with (
        action_scope(clinic_id=clinic_id, principal_id=principal_id) as using,
        connections[using].cursor() as cursor,
    ):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-send-v1',0))"
            if principal_id is None
            else "SELECT pg_advisory_lock(hashtextextended('agent-send-v1',0))"
        )
    try:
        yield using
    finally:
        if principal_id is not None:
            with connections[using].cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended('agent-send-v1',0))"
                )
