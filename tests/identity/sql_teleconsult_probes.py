"""Teleconsult device-check trigger oracle: fire the guarded INSERT, never call it.

``clinic_app.teleconsult_binding_guard``'s device-check branch decides staff
participant authority through ``teleconsult_clinician``: todo 6's
``has_permission('clinical.write', clinic, session patient's enrollment)`` and
the session's bound physician. The invalid subject is an unbound actor, so the
refusal is the same 42501 the trigger raises for every other non-participant.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
from apps.teleconsult.models import TeleconsultDeviceCheck
from django.db import DatabaseError, connection

if TYPE_CHECKING:
    from collections.abc import Iterator

    from identity.sql_guard_probes import SqlWorld

REFUSAL = "teleconsult participant authority required"


@contextmanager
def trigger_refusal() -> Iterator[None]:
    """A 42501 must come from the trigger, the first layer, not from RLS.

    The insert policy repeats the decision; without this, removing the
    trigger's authority check would still pass on the policy's 42501.
    """
    try:
        yield
    except DatabaseError as error:
        cause = error.__cause__
        if getattr(cause, "sqlstate", None) == "42501":
            assert isinstance(cause, psycopg.Error)
            assert cause.diag.message_primary == REFUSAL
        raise


def insert_device_check(w: SqlWorld, valid: bool) -> bool:
    """The acting role reports its own physician-side device check."""
    actor = w.actor.actor.pk
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(uuid4())],
            )
    with trigger_refusal():
        TeleconsultDeviceCheck.objects.create(
            organization_id=w.actor.graph.organization_a,
            session_id=w.teleconsult.session.pk,
            role="physician",
            participant_id=actor,
            camera="ok",
            microphone="ok",
            speaker="ok",
            network="good",
        )
    return True
