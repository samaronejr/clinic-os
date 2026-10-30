"""Populated reversal/forward preserves catalog authority and immutable history."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from apps.audit.services import verify_chain
from apps.intake.patient_access import patient_session_context
from apps.teleconsult import participants
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_mode_audit import second_patient_session
from teleconsult.test_mode_transition import mode_history
from teleconsult.test_participants import seed_world

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
OLD = [("teleconsult", "0005_event_authority")]


def mode_migrations(*, backwards: bool) -> None:
    """Exercise all reversible mode DDL without rolling back protected transcripts."""
    loader = MigrationExecutor(connection).loader
    names = ("0006_mode_audit", "0007_consented_capture")
    with connection.schema_editor() as editor:
        for name in reversed(names) if backwards else names:
            migration = loader.get_migration("teleconsult", name)
            state = loader.project_state(migration.dependencies)
            if backwards:
                migration.unapply(state, editor)
            else:
                migration.apply(state, editor)


def catalog_hash() -> str:
    """Include bodies, owners, ACLs, definer flags, volatility and search path."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname,pg_get_functiondef(p.oid),"
            "pg_get_userbyid(p.proowner),p.proacl::text,p.proconfig,"
            "p.prosecdef,p.provolatile,p.proparallel,p.prorettype::regtype::text "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='clinic_app' AND p.proname IN "
            "('teleconsult_mode_transition_scope','teleconsult_mode_transition_audit',"
            "'teleconsult_binding_guard','teleconsult_immutable') "
            "ORDER BY p.proname"
        )
        return hashlib.sha256(repr(cursor.fetchall()).encode()).hexdigest()


def history_hash(organization: UUID) -> str:
    """Hash all audit rows and event content, including immutable receipts."""
    with setup_context(organization), connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT jsonb_agg(to_jsonb(a) ORDER BY a.seq) "
            "FROM clinic_app.audit_event a WHERE a.organization_id=%s),"
            "(SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) "
            "FROM clinic_app.teleconsult_teleconsultevent e "
            "WHERE e.organization_id=%s)",
            [str(organization), str(organization)],
        )
        return hashlib.sha256(repr(cursor.fetchone()).encode()).hexdigest()


def test_populated_mode_migration_reverses_and_reapplies_exact_catalog(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    second = second_patient_session(w)
    with runtime_role(), patient_session_context(w.patient_session):
        participants.set_patient_audio_only(session_id=w.session, enabled=True)
    original = history_hash(w.organization)
    new_catalog = catalog_hash()
    executor = MigrationExecutor(connection)
    installed = frozenset(executor.loader.applied_migrations)
    # A graph rollback now crosses protected transcript storage. Prove that it
    # refuses, then test the exact reversible teleconsult migrations in place.
    with pytest.raises(IrreversibleError):
        executor.migrate(OLD)
    assert catalog_hash() == new_catalog
    assert history_hash(w.organization) == original
    reversed_modes = False
    try:
        mode_migrations(backwards=True)
        reversed_modes = True
        old_catalog = catalog_hash()
        assert history_hash(w.organization) == original
        with setup_context(w.organization):
            verify_chain(w.organization)
        mode_migrations(backwards=False)
        reversed_modes = False
        assert catalog_hash() == new_catalog
        assert history_hash(w.organization) == original
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname='teleconsult_mode_audit_binding'"
            )
            assert cursor.fetchone() == (True,)
        mode_migrations(backwards=True)
        reversed_modes = True
        assert catalog_hash() == old_catalog
        assert history_hash(w.organization) == original
        mode_migrations(backwards=False)
        reversed_modes = False
        assert catalog_hash() == new_catalog
        assert history_hash(w.organization) == original
    finally:
        if reversed_modes:
            mode_migrations(backwards=False)
    assert (
        frozenset(MigrationExecutor(connection).loader.applied_migrations) == installed
    )
    with runtime_role(), patient_session_context(second):
        participants.set_patient_audio_only(session_id=w.session, enabled=False)
    events, audits = mode_history(w)
    assert [event.kind for event in events] == ["audio_only", "video_restored"]
    assert [audit.actor_user_id for audit in audits] == [w.patient_session, second]
    assert [event.mode_audit_seq for event in events] == [audit.seq for audit in audits]
