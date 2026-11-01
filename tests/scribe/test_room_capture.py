"""Keep room capture behind the same live recording authority as chunk ingest."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_ai_disclosure,
    record_consent,
)
from apps.intake.patient_access import patient_session_context
from apps.scribe.services import (
    control_recording,
    create_recording,
    issue_encounter_token,
)
from apps.teleconsult.models import TeleconsultRoom
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, transaction

from ehr.test_autosave import registration
from patient_service_support import runtime_role
from provider_gate_support import seed_capabilities
from renewal.test_teleconsult_sessions import (
    _create,
    _run_room,
    seed,
    synthetic_provider,
)
from scribe.test_ingest import media

if TYPE_CHECKING:
    from apps.teleconsult.adapters import SyntheticRoomAdapter

    from rbac_fixtures import RbacGraph

__all__ = ("media", "synthetic_provider")
pytestmark = pytest.mark.django_db(transaction=True)


def test_room_bits_require_explicit_live_capture_and_asr_stays_closed(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
) -> None:
    graph = rbac_graph
    seed_capabilities()
    registration(graph, graph.physician, graph.clinic_a)
    _appointment, encounter, _consent, patient, manager = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        room = TeleconsultRoom.objects.get(session_id=session.pk)
        for flag in ("recording_enabled", "transcription_enabled"):
            with pytest.raises(DatabaseError), transaction.atomic():
                TeleconsultRoom.objects.filter(pk=room.pk).update(**{flag: True})
        room.refresh_from_db()
        assert (room.recording_enabled, room.transcription_enabled) == (False, False)
    with runtime_role(), tenant_context(manager, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="consultation_recording",
            text="Autorização sintética específica para gravação.",
        )
    with runtime_role(), patient_session_context(patient):
        _text, offer = prepare_acceptance(text_id=text.pk)
        record_consent(offer=offer, purpose="consultation_recording", accepted=True)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            informed=True,
            refused=False,
        )
        token = issue_encounter_token(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            browser_binding="synthetic-room-capture",
        )
        recording = create_recording(
            clinic_id=graph.clinic_a, encounter_token=token, command_id=uuid4()
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultRoom.objects.filter(pk=room.pk).update(recording_enabled=True)
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="start",
            expected_epoch=0,
        )
        assert (
            TeleconsultRoom.objects.filter(pk=room.pk).update(recording_enabled=True)
            == 1
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultRoom.objects.filter(pk=room.pk).update(
                transcription_enabled=True
            )
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="pause",
            expected_epoch=1,
        )
        room.refresh_from_db()
        assert (room.recording_enabled, room.transcription_enabled) == (False, False)
