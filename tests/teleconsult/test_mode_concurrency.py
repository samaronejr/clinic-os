"""Contended mode INSERTs and writer replays retain one actual audit origin."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from apps.intake.patient_access import patient_session_context
from apps.tenancy.db import tenant_context
from django.db import connections

from patient_service_support import runtime_role
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_mode_audit import second_patient_session
from teleconsult.test_mode_transition import mode_history, sql_mode
from teleconsult.test_participants import seed_world

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph
    from teleconsult.test_mode_transition import Role

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("cohort", ["patient", "physician", "mixed"])
def test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority(
    rbac_graph: RbacGraph, synthetic_provider: object, cohort: str
) -> None:
    w = seed_world(rbac_graph)
    second = second_patient_session(w)
    cohorts: dict[str, list[tuple[Role, UUID]]] = {
        "patient": [("patient", w.patient_session), ("patient", second)],
        "physician": [
            ("physician", w.graph.physician),
            ("physician", w.graph.physician),
        ],
        "mixed": [("patient", second), ("physician", w.graph.physician)],
    }
    subjects = cohorts[cohort]
    ready = Barrier(2, timeout=10)

    def call(subject: tuple[Role, UUID]) -> tuple[UUID, tuple[int, int] | None]:
        role, actor = subject
        connections.close_all()
        try:
            authority = (
                patient_session_context(actor)
                if role == "patient"
                else tenant_context(actor, w.organization)
            )
            with runtime_role(), authority:
                ready.wait()
                return actor, sql_mode(w, role, enabled=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, subject) for subject in subjects]
        results = [future.result(timeout=15) for future in futures]
    winners = [(actor, result) for actor, result in results if result is not None]
    expected = 2 if cohort == "mixed" else 1
    assert len(winners) == expected
    events, audits = mode_history(w)
    assert len(events) == len(audits) == expected
    by_event = {event.pk: event for event in events}
    by_audit = {audit.seq: audit for audit in audits}
    for actor, receipt in winners:
        assert receipt is not None
        event_id, audit_seq = receipt
        assert by_event[event_id].mode_audit_seq == audit_seq
        assert by_audit[audit_seq].actor_user_id == actor
        assert by_audit[audit_seq].payload["object_verb"] == "audio_only"


def test_concurrent_mode_replays_from_two_valid_sessions_write_nothing(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    second = second_patient_session(w)
    with runtime_role(), patient_session_context(w.patient_session):
        sql_mode(w, "patient", enabled=True)
    before_events, before_audits = mode_history(w)
    ready = Barrier(2, timeout=10)

    def replay(actor: UUID) -> tuple[int, int] | None:
        connections.close_all()
        try:
            with runtime_role(), patient_session_context(actor):
                ready.wait()
                return sql_mode(w, "patient", enabled=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(replay, actor) for actor in (w.patient_session, second)]
        assert [future.result(timeout=15) for future in futures] == [None, None]
    events, audits = mode_history(w)
    assert [event.pk for event in events] == [event.pk for event in before_events]
    assert [audit.seq for audit in audits] == [audit.seq for audit in before_audits]
    assert [audit.actor_user_id for audit in audits] == [w.patient_session]
