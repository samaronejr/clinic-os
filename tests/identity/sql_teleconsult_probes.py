"""Teleconsult device-check trigger oracle: fire the guarded INSERT, never call it.

``clinic_app.teleconsult_binding_guard``'s device-check branch decides staff
participant authority through ``teleconsult_clinician``: todo 6's
``has_permission('clinical.write', clinic, session patient's enrollment)`` and
the session's bound physician. The invalid subject is an unbound actor, so the
refusal is the same 42501 the trigger raises for every other non-participant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.teleconsult.models import TeleconsultDeviceCheck, TeleconsultEvent
from django.db import connection

from identity.legacy_owner_boundaries import _owner_call
from identity.sql_denial_contracts import DmlMember, SqlVerdict, probe, probe_dml

if TYPE_CHECKING:
    from identity.sql_guard_probes import SqlWorld

DEVICE_MEMBER = DmlMember(
    "clinic_app.teleconsult_binding_guard",
    "clinic_app.teleconsult_teleconsultdevicecheck",
    "INSERT",
    "teleconsult_teleconsultdevicecheck",
)
MODE_MEMBER = DmlMember(
    "clinic_app.teleconsult_binding_guard",
    "clinic_app.teleconsult_teleconsultevent",
    "INSERT",
    "teleconsult_teleconsultevent",
)


def insert_device_check(w: SqlWorld, valid: bool) -> SqlVerdict:
    """The acting role reports its own physician-side device check."""
    actor = w.actor.actor.pk
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(uuid4())],
            )
    return probe_dml(
        DEVICE_MEMBER,
        TeleconsultDeviceCheck(
            organization_id=w.actor.graph.organization_a,
            session_id=w.teleconsult.session.pk,
            role="physician",
            participant_id=actor,
            camera="ok",
            microphone="ok",
            speaker="ok",
            network="good",
        ),
    )


def mode_transition_scope(w: SqlWorld, valid: bool) -> SqlVerdict:
    """The resolver's private scope keeps the actual staff GUCs."""
    result = _owner_call(
        lambda: probe(
            "clinic_app.teleconsult_mode_transition_scope",
            [w.teleconsult.session.pk if valid else uuid4(), "physician"],
        )
    )
    assert isinstance(result, SqlVerdict)
    return result


def insert_mode_event(w: SqlWorld, valid: bool) -> SqlVerdict:
    """Fire the event binding guard and witness its real bigint-identified row."""
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id',%s,true)",
                [str(uuid4())],
            )
    return probe_dml(
        MODE_MEMBER,
        TeleconsultEvent(
            organization_id=w.actor.graph.organization_a,
            session_id=w.teleconsult.session.pk,
            actor_role="physician",
            kind="audio_only",
        ),
    )
