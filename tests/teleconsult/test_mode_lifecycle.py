"""Patient mode changes share lock admission with physician lifecycle writes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from typing import TYPE_CHECKING, Literal

import pytest
from apps.intake.patient_access import patient_session_context
from apps.teleconsult import participants, services
from apps.tenancy.db import tenant_context
from django.db import connection, connections

from patient_service_support import runtime_role
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_mode_transition import mode_history
from teleconsult.test_participants import seed_world

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
type Lifecycle = Literal["end", "remove"]


@pytest.mark.parametrize("lifecycle", ["end", "remove"])
def test_patient_toggle_racing_lifecycle_has_no_lock_inversion(
    rbac_graph: RbacGraph,
    synthetic_provider: object,
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: Lifecycle,
) -> None:
    w = seed_world(rbac_graph)
    ready = Barrier(2, timeout=10)
    physician_admitted = Event()
    original_lock = services._lock_session

    def admitted_lock(key: UUID, **scope: UUID) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT NULLIF(current_setting('app.current_patient_session',true),'')"
            )
            row = cursor.fetchone()
        assert row is not None
        ready.wait()
        if row[0] is not None:
            # The physician has obtained its first lock before this caller
            # proceeds. Before the fix, the patient already holds the audit
            # lock here: the two real transactions then form a 40P01 cycle.
            assert physician_admitted.wait(timeout=10)
            original_lock(key, **scope)
        else:
            original_lock(key, **scope)
            physician_admitted.set()

    monkeypatch.setattr(services, "_lock_session", admitted_lock)
    monkeypatch.setattr(participants, "_lock_session", admitted_lock)

    def patient() -> str:
        connections.close_all()
        try:
            with runtime_role(), patient_session_context(w.patient_session):
                try:
                    participants.set_patient_audio_only(
                        session_id=w.session, enabled=True
                    )
                except services.TeleconsultConflictError as error:
                    return error.reason_code
            return "changed"
        finally:
            connections.close_all()

    def physician() -> str:
        connections.close_all()
        try:
            with (
                runtime_role(),
                tenant_context(w.graph.physician, w.organization),
            ):
                if lifecycle == "end":
                    services.end_consultation(clinic_id=w.clinic, session_id=w.session)
                else:
                    participants.remove_patient(
                        clinic_id=w.clinic, session_id=w.session
                    )
            return lifecycle
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        patient_call = pool.submit(patient)
        physician_call = pool.submit(physician)
        assert physician_call.result(timeout=15) == lifecycle
        assert patient_call.result(timeout=15) in {
            "session_closed",
            "not_in_room",
        }
    events, audits = mode_history(w)
    assert events == []
    assert audits == []
