"""The additive event guard reverses exactly without losing v2 history."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.teleconsult import participants
from apps.teleconsult.migrations._teleconsult_v2_sql import BINDING_GUARD_V2
from apps.teleconsult.models import TeleconsultEvent
from apps.tenancy.db import tenant_context
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from consent.test_authority import as_owner
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_participants import seed_world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)


def guard_definition() -> tuple[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_get_functiondef(oid), prosrc "
            "FROM pg_catalog.pg_proc "
            "WHERE oid='clinic_app.teleconsult_binding_guard()'::regprocedure"
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def test_event_authority_reverse_is_exact_with_populated_v2_events(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        participants.set_audio_only(
            clinic_id=w.clinic, session_id=w.session, enabled=True
        )
    before, _ = guard_definition()
    with as_owner():
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("teleconsult", "0004_teleconsult_v2")])
            _, restored_body = guard_definition()
            assert restored_body == BINDING_GUARD_V2.split("$f$")[1]
            with setup_context(w.organization):
                assert (
                    TeleconsultEvent.objects.filter(
                        session_id=w.session, kind="audio_only"
                    ).count()
                    == 1
                )
        finally:
            MigrationExecutor(connection).migrate(targets)
    after, _ = guard_definition()
    assert after == before
