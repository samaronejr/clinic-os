"""Receipt order is independent of equal clocks and preallocated event IDs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.intake.patient_access import patient_session_context
from apps.teleconsult import participants
from django.db import connection
from django.utils import timezone

from patient_service_support import runtime_role
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_mode_transition import mode_history
from teleconsult.test_participants import seed_world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_preallocated_ids_and_equal_clocks_cannot_reorder_committed_modes(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    with runtime_role(), patient_session_context(w.patient_session):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT nextval('clinic_app.teleconsult_teleconsultevent_id_seq') "
                "FROM generate_series(1,2)"
            )
            low, high = (int(row[0]) for row in cursor.fetchall())
            cursor.execute(
                "INSERT INTO clinic_app.teleconsult_teleconsultevent "
                "(id,organization_id,session_id,kind,actor_role,reason_code,"
                "created_at) SELECT v.id,%s,%s,v.kind,'patient','',%s "
                "FROM (VALUES (%s,'audio_only'),(%s,'video_restored')) v(id,kind) "
                "RETURNING id,mode_audit_seq,created_at",
                [
                    str(w.organization),
                    str(w.session),
                    timezone.now(),
                    high,
                    low,
                ],
            )
            rows = cursor.fetchall()
        assert [row[0] for row in rows] == [high, low]
        assert rows[0][2] == rows[1][2]
        session = participants.patient_session(session_id=w.session)
        assert participants.audio_only(session, "patient") is False
        assert participants.set_patient_audio_only(session_id=w.session, enabled=True)
    events, audits = mode_history(w)
    assert [event.kind for event in events] == [
        "audio_only",
        "video_restored",
        "audio_only",
    ]
    assert len(audits) == 3
