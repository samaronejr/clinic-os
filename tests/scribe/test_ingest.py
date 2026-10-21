"""Recording identity, duplicate quarantine, epochs, consent and key custody."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from hashlib import sha256
from secrets import token_hex
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID, uuid4

import pytest
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_consent,
    revoke_consent,
)
from apps.core.integration import execute_operation
from apps.ehr.services import open_encounter
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.scribe import ingest
from apps.scribe.contracts import ChunkInput, ScribeError
from apps.scribe.models import AudioChunk, CaptureEpoch
from apps.scribe.services import (
    control_recording,
    create_recording,
    ingest_chunk,
    issue_encounter_token,
)
from apps.scribe.storage import open_audio
from apps.tenancy import envelope
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from ehr.test_autosave import registration
from patient_service_support import runtime_role
from provider_gate_support import seed_capabilities
from renewal.test_encounters import seed, setup_context
from renewal.test_encryption_readiness import _owner_scope

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

BODY = b"SINTETICO-SENTINELA-40-audio"
CODEC = "audio/webm;codecs=opus"


class ChunkMutation(TypedDict, total=False):
    sha256: str
    data: bytes
    duration_ms: int
    seq: int
    codec: str
    track: str


@dataclass(frozen=True, slots=True)
class World:
    session: UUID
    encounter: UUID
    patient_session: UUID
    acceptance: UUID
    token: str = field(repr=False)
    epoch: UUID


@pytest.fixture(autouse=True)
def media(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.SCRIBE_SYNTHETIC_CAPTURE = True
    settings.SCRIBE_MEDIA_ROOT = tmp_path / "media"


def world(graph: RbacGraph) -> World:
    seed_capabilities()
    registration(graph, graph.physician, graph.clinic_a)
    appointment, _template = seed(graph)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="consultation_recording",
            text="Consentimento sintético específico para gravação.",
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    with runtime_role():
        patient_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert patient_session is not None
    with runtime_role(), patient_session_context(patient_session):
        _text, offer = prepare_acceptance(text_id=text.pk)
        acceptance = record_consent(
            offer=offer, purpose="consultation_recording", accepted=True
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
        token = issue_encounter_token(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            browser_binding="synthetic-browser-binding",
        )
        recording = create_recording(
            clinic_id=graph.clinic_a,
            encounter_token=token,
            command_id=uuid4(),
        )
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="start",
            expected_epoch=0,
        )
        epoch_id = CaptureEpoch.objects.get(session=recording, number=1).pk
    return World(
        recording.pk, encounter.pk, patient_session, acceptance.pk, token, epoch_id
    )


def chunk(scope: World, *, seq: int = 0, data: bytes = BODY) -> ChunkInput:
    return ChunkInput(
        session_id=scope.session,
        epoch=scope.epoch,
        track="microphone",
        seq=seq,
        sha256=sha256(data).hexdigest(),
        duration_ms=2000,
        codec=CODEC,
        data=data,
    )


def receive(graph: RbacGraph, scope: World, value: ChunkInput) -> str:
    return ingest_chunk(
        clinic_id=graph.clinic_a, encounter_token=scope.token, chunk=value
    ).ack


def test_identical_replays_converge_without_resetting_expiry(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a live recording with one exact chunk.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert receive(graph, scope, chunk(scope)) == "received"
        original = AudioChunk.objects.get(session_id=scope.session)
        # When: the acknowledged upload is replayed twice.
        acknowledgements = [receive(graph, scope, chunk(scope)) for _ in range(2)]
        # Then: the replay result and original expiry are stable.
        assert acknowledgements == ["duplicate", "duplicate"]
        stored = AudioChunk.objects.get(pk=original.pk)
        assert stored.expires_at == original.expires_at
        assert stored.storage_key == original.storage_key
        assert AudioChunk.objects.filter(session_id=scope.session).count() == 1


def test_conflicting_duplicate_quarantines_original_without_replacement(
    rbac_graph: RbacGraph,
) -> None:
    # Given: one durably received slice.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receive(graph, scope, chunk(scope))
        original = AudioChunk.objects.get(session_id=scope.session)
        # When: the same identity names different bytes.
        result = ingest_chunk(
            clinic_id=graph.clinic_a,
            encounter_token=scope.token,
            chunk=chunk(scope, data=b"different synthetic bytes"),
        )
        # Then: neither variant becomes usable audio.
        original.refresh_from_db()
        assert (result.ack, result.code) == ("rejected", "conflicting_duplicate")
        assert original.state == "quarantined"
        assert original.sha256 == sha256(BODY).hexdigest()
        assert original.wrapped_key == b""


def test_reordered_chunks_close_only_the_gap_they_fill(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a server-issued capture epoch.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receive(graph, scope, chunk(scope, seq=2))
        epoch = CaptureEpoch.objects.get(session_id=scope.session, number=1)
        assert epoch.gaps == {"microphone": [[0, 1]]}
        # When: one missing slice arrives out of order.
        receive(graph, scope, chunk(scope))
        # Then: the remaining gap stays explicit.
        epoch.refresh_from_db()
        assert epoch.gaps == {"microphone": [[1, 1]]}


def test_resume_fences_old_epoch_and_commands_replay(
    rbac_graph: RbacGraph,
) -> None:
    # Given: an explicitly paused recording.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="pause",
            expected_epoch=1,
        )
        command = uuid4()
        resume = partial(
            control_recording,
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=command,
            action="resume",
            expected_epoch=1,
        )
        # When: a resume command is retried.
        first = resume()
        second = resume()
        # Then: only one new epoch exists and the previous one is stale.
        assert first == second
        assert first.epoch == 2
        result = ingest_chunk(
            clinic_id=graph.clinic_a,
            encounter_token=scope.token,
            chunk=chunk(scope),
        )
        assert (result.ack, result.code) == ("rejected", "epoch_stale")


def test_revoked_consent_stops_new_chunks_and_direct_sql(
    rbac_graph: RbacGraph,
) -> None:
    # Given: consent was withdrawn after the capture epoch was issued.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), patient_session_context(scope.patient_session):
        revoke_consent(acceptance_id=scope.acceptance)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # When: the browser attempts to upload another slice.
        result = ingest_chunk(
            clinic_id=graph.clinic_a,
            encounter_token=scope.token,
            chunk=chunk(scope),
        )
        # Then: no audio or outbox row is created.
        assert (result.ack, result.code) == ("rejected", "consent_revoked")
        assert not AudioChunk.objects.filter(session_id=scope.session).exists()
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.scribe_recordingsession SET state='recording' "
                "WHERE id=%s",
                [scope.session],
            )


@pytest.mark.parametrize(
    "mutation",
    [
        {"sha256": "0" * 64},
        {"data": b"x" * (256 * 1024 + 1)},
        {"duration_ms": 0},
        {"duration_ms": 5001},
        {"seq": -1},
        {"codec": "audio/mpeg"},
        {"track": "patient-name"},
    ],
)
def test_invalid_chunks_leave_no_rows(
    rbac_graph: RbacGraph, mutation: ChunkMutation
) -> None:
    # Given: an otherwise valid authorized recording.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # When: one untrusted field violates the bounded ingest contract.
        with pytest.raises(ScribeError):
            receive(graph, scope, replace(chunk(scope), **mutation))
        # Then: no invalid slice was acknowledged or staged.
        assert not AudioChunk.objects.filter(session_id=scope.session).exists()


def test_outbox_stores_only_ciphertext_and_object_keys_are_not_authority(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a received slice whose remote write has not run in the request.
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receive(graph, scope, chunk(scope))
        stored = AudioChunk.objects.get(session_id=scope.session)
        assert BODY not in bytes(stored.encrypted_data)
        operation = stored.operation_id
    assert operation is not None
    # When: the committed outbox effect executes outside the request transaction.
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    # Then: the assigned clinician can read the exact authenticated bytes.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert open_audio(clinic_id=graph.clinic_a, chunk_id=stored.pk) == BODY
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_b),
        pytest.raises(ScribeError),
    ):
        open_audio(clinic_id=graph.clinic_c, chunk_id=stored.pk)
    with setup_context(graph.organization_a):
        assert AudioChunk.objects.get(pk=stored.pk).state == "accepted"


def test_duplicate_after_deadline_is_a_read_only_original_ack(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert receive(graph, scope, chunk(scope)) == "received"
        original = AudioChunk.objects.get(session_id=scope.session)
        monkeypatch.setattr(
            ingest, "utc_now", lambda: original.expires_at + timedelta(seconds=10)
        )
        assert receive(graph, scope, chunk(scope)) == "duplicate"
        original.refresh_from_db()
        assert original.expires_at == (
            CaptureEpoch.objects.get(pk=scope.epoch).started_at + timedelta(seconds=60)
        )
        assert AudioChunk.objects.filter(session_id=scope.session).count() == 1


def test_discard_destroys_only_the_audio_object_keys(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receive(graph, scope, chunk(scope))
        stored = AudioChunk.objects.get(session_id=scope.session)
        operation = stored.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert open_audio(clinic_id=graph.clinic_a, chunk_id=stored.pk) == BODY
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="cancel",
            expected_epoch=1,
        )
        stored.refresh_from_db()
        assert (stored.state, bytes(stored.wrapped_key)) == ("rejected", b"")
        with pytest.raises(ScribeError):
            open_audio(clinic_id=graph.clinic_a, chunk_id=stored.pk)
        assert CaptureEpoch.objects.get(pk=scope.epoch).ended_at is not None


def test_existing_audio_survives_kek_rewrap_without_rewriting_the_object(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receive(graph, scope, chunk(scope))
        stored = AudioChunk.objects.get(session_id=scope.session)
        operation = stored.operation_id
        wrapped = bytes(stored.wrapped_key)
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    next_kek = token_hex(32)
    with _owner_scope(graph.organization_a, graph.physician):
        rewrapped = envelope.rewrap_tenant_keys(new_kek=next_kek)
        assert rewrapped >= 1
    monkeypatch.setattr(envelope, "_kek", lambda: next_kek)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert open_audio(clinic_id=graph.clinic_a, chunk_id=stored.pk) == BODY
        stored.refresh_from_db()
        assert bytes(stored.wrapped_key) == wrapped
