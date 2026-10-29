"""Protected graph rollback refuses; reversible event DDL retains v2 history."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.teleconsult import participants
from apps.teleconsult.migrations._teleconsult_v2_sql import BINDING_GUARD_V2
from apps.teleconsult.models import TeleconsultEvent
from apps.tenancy.db import tenant_context
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from consent.test_authority import as_owner
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_mode_migration import catalog_hash, history_hash
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
    original_catalog = catalog_hash()
    original_history = history_hash(w.organization)
    with as_owner():
        executor = MigrationExecutor(connection)
        installed = frozenset(executor.loader.applied_migrations)
        # Protected transcript/note storage is a descendant of these guards.
        # A graph rollback must refuse without touching the catalog or history.
        with pytest.raises(IrreversibleError):
            executor.migrate([("teleconsult", "0004_teleconsult_v2")])
        assert guard_definition()[0] == before
        assert catalog_hash() == original_catalog
        assert history_hash(w.organization) == original_history
        reversed_names: list[str] = []
        try:
            # Exercise only the reversible teleconsult operations in dependency
            # order, leaving all protected descendants and recorder rows intact.
            for name in (
                "0007_consented_capture",
                "0006_mode_audit",
                "0005_event_authority",
            ):
                migration = executor.loader.get_migration("teleconsult", name)
                state = executor.loader.project_state(migration.dependencies)
                with connection.schema_editor() as editor:
                    migration.unapply(state, editor)
                reversed_names.append(name)
            _, restored_body = guard_definition()
            assert restored_body == BINDING_GUARD_V2.split("$f$")[1]
            assert history_hash(w.organization) == original_history
            with setup_context(w.organization):
                assert (
                    TeleconsultEvent.objects.filter(
                        session_id=w.session, kind="audio_only"
                    ).count()
                    == 1
                )
        finally:
            for name in reversed(reversed_names):
                migration = executor.loader.get_migration("teleconsult", name)
                state = executor.loader.project_state(migration.dependencies)
                with connection.schema_editor() as editor:
                    migration.apply(state, editor)
        assert (
            frozenset(MigrationExecutor(connection).loader.applied_migrations)
            == installed
        )
    after, _ = guard_definition()
    assert after == before
    assert catalog_hash() == original_catalog
    assert history_hash(w.organization) == original_history
