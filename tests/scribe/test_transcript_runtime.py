"""Real-role transcript acceptance; run only in the serialized database window."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai.contracts import ProviderRequest, ProviderUnavailableError, Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.policy import OrganizationPolicy
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_consent,
    revoke_consent,
)
from apps.core.integration import execute_operation
from apps.core.patient_context import SESSION_PREFIX
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import User
from apps.intake.patient_access import patient_session_context
from apps.providers.lifecycle import VersionInput, propose_version
from apps.scribe.models import (
    AudioChunk,
    ChunkTranscription,
    RecordingSession,
    TranscriptSegment,
    TranscriptVersion,
)
from apps.scribe.outbox import register_gateway
from apps.scribe.services import (
    control_recording,
    finalize_transcript,
    ingest_chunk,
    read_transcript,
    review_segment,
)
from apps.scribe.tasks import run_transcription
from apps.scribe.transcript_contracts import SegmentReview, TranscriptError
from apps.scribe.transcript_views import transcript_review
from apps.scribe.transcripts import coverage_for
from apps.tenancy.db import tenant_context
from django.contrib.sessions.backends.db import SessionStore
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory

from ai.support import model, seed
from patient_service_support import runtime_role
from scribe.test_ingest import World, chunk, media, world

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scribe.gateway import ScribeSyntheticProvider

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def admit_interactive_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.scribe.tasks.fair_acquire", lambda **_kwargs: True)


def permit_asr(graph: RbacGraph, scope: World, monkeypatch: pytest.MonkeyPatch) -> UUID:
    gateway = seed(graph, monkeypatch)
    register_gateway()
    propose_version(
        "asr",
        version_input=VersionInput(
            provider="synthetic",
            account=f"synthetic-asr-{uuid4().hex}",
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    model(
        organization=graph.organization_a,
        clinic=graph.clinic_a,
        purpose=Purpose.SCRIBE_ASR,
    )
    with runtime_role(), tenant_context(gateway.administrator, graph.organization_a):
        current = latest_configuration(graph.clinic_a)
        assert current is not None
        set_organization_policy(
            clinic_id=graph.clinic_a,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=("scribe_asr",),
                monthly_budget_micros=10_000_000,
                purpose_budgets=(("scribe_asr", 10_000_000),),
            ),
        )
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="ai_assistance",
            text="Consentimento sintético para transcrição assistida.",
        )
    with runtime_role(), patient_session_context(scope.patient_session):
        _text, offer = prepare_acceptance(text_id=text.pk)
        acceptance = record_consent(offer=offer, purpose="ai_assistance", accepted=True)
    return acceptance.pk


def accepted(
    graph: RbacGraph,
    scope: World,
    *,
    sequence: int = 0,
    track: str = "microphone",
    fixture_index: int | None = None,
) -> UUID:
    fixture = sequence % 4 if fixture_index is None else fixture_index
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert (
            ingest_chunk(
                clinic_id=graph.clinic_a,
                encounter_token=scope.token,
                chunk=replace(
                    chunk(
                        scope,
                        seq=sequence,
                        data=f"SINTETICO-ASR-FIXTURE:{fixture}".encode("ascii"),
                    ),
                    track=track,
                ),
            ).ack
            == "received"
        )
        operation = AudioChunk.objects.get(
            session_id=scope.session, seq=sequence, track=track
        ).operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    return operation


def test_recording_consent_alone_never_dispatches_asr(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    scope = world(graph)
    operation = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "transcription_pending"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not ChunkTranscription.objects.filter(session_id=scope.session).exists()
        assert AudioChunk.objects.get(session_id=scope.session).wrapped_key


def test_governed_completion_review_and_final_are_retained_successors(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        request = ChunkTranscription.objects.select_related("invocation").get(
            session_id=scope.session,
        )
        source = json.loads(request.invocation.inputs_ref.content)
        assert source["schema"] == "scribe.audio-reference.v1"
        assert "audio_b64" not in source
        inference_operation = request.invocation.operation_id
    assert inference_operation is not None
    with runtime_role():
        assert execute_operation(inference_operation) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        current = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
        assert current is not None
        assert current.revision == 2
        assert current.segments[0].speaker == "unknown"
        updated = review_segment(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=current.revision,
            review=SegmentReview(current.segments[0].segment_id, speaker="caregiver"),
        )
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=0,
        )
        final = finalize_transcript(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=updated.revision,
            acknowledge_incomplete=False,
        )
        assert final.state == "final"
        assert current.segments[0].speaker == "unknown"
        assert final.segments[0].speaker == "caregiver"
        assert TranscriptVersion.objects.filter(session_id=scope.session).count() == 4
        assert TranscriptSegment.objects.filter(session_id=scope.session).count() == 3
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.scribe_transcriptversion SET state='partial' "
                "WHERE id=%s",
                [final.version_id],
            )
    with psycopg.connect(superuser_database_url) as inspection:
        rows = inspection.execute(
            "SELECT affected_record_type,payload->>'object_verb' "
            "FROM clinic_app.audit_event WHERE affected_record_id IN (%s,%s)",
            [str(final.version_id), str(updated.version_id)],
        ).fetchall()
    assert set(rows) == {
        ("scribe.transcript", "finalized"),
        ("scribe.transcript", "speaker_relabeled"),
    }


def test_read_unknown_and_foreign_clinic_have_the_same_denial(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    messages: list[str] = []
    for clinic, recording in (
        (graph.clinic_b, scope.session),
        (graph.clinic_a, uuid4()),
    ):
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            with pytest.raises(PermissionError) as error:
                read_transcript(clinic_id=clinic, session_id=recording)
            messages.append(str(error.value))
    assert messages[0] == messages[1]


def test_recorded_visit_final_preserves_inaudibility_and_ambiguous_dose(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    # Duplex capture keeps every slice within both clocks' five-second fence,
    # even when the standalone QA runs immediately after epoch creation.
    # Reorder both tracks; the pure fixture test covers all 24 permutations.
    for sequence, track, fixture in (
        (1, "microphone", 2),
        (0, "microphone", 0),
        (1, "remote", 3),
        (0, "remote", 1),
    ):
        operation = accepted(
            graph, scope, sequence=sequence, track=track, fixture_index=fixture
        )
        with runtime_role():
            assert run_transcription(operation_id=str(operation)) == "queued"
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            inference = ChunkTranscription.objects.get(
                session_id=scope.session,
                chunk__seq=sequence,
                chunk__track=track,
            ).invocation.operation_id
        assert inference is not None
        with runtime_role():
            assert execute_operation(inference) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
        current = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
        assert current is not None
        assert current.revision == 8
        final = finalize_transcript(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=current.revision,
            acknowledge_incomplete=False,
        )
        assert final.state == "final"
        assert not final.missing
        by_text = {segment.text: segment for segment in final.segments}
        assert set(by_text) == {
            "sim sim",
            "[inaudível]",
            "5 ou 15 mg",
            "Revisão sintética.",
        }
        assert [span.kind for span in by_text["[inaudível]"].uncertainty] == [
            "inaudible"
        ]
        assert [span.kind for span in by_text["5 ou 15 mg"].uncertainty] == [
            "uncertain"
        ]
        assert all(segment.speaker == "unknown" for segment in final.segments)
        assert [segment.start_ms for segment in final.segments] == sorted(
            segment.start_ms for segment in final.segments
        )
        assert TranscriptVersion.objects.filter(session_id=scope.session).count() == 9


@pytest.mark.parametrize("finalized", [False, True])
def test_late_audio_refreshes_coverage_without_changing_saved_versions(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    finalized: bool,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
        assert run_transcription(operation_id=str(operation)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        admission = ChunkTranscription.objects.get(session_id=scope.session)
        inference = admission.invocation.operation_id
    assert inference is not None
    with runtime_role():
        assert execute_operation(inference) == "succeeded"
        assert execute_operation(inference) == "skipped"
        assert run_transcription(operation_id=str(operation)) == "complete"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        saved = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
        assert saved is not None
        assert saved.revision == 2
        assert not saved.missing
        assert ChunkTranscription.objects.filter(session_id=scope.session).count() == 1
        assert (
            ingest_chunk(
                clinic_id=graph.clinic_a,
                encounter_token=scope.token,
                chunk=chunk(scope, seq=1),
            ).ack
            == "received"
        )
        pending_operation = AudioChunk.objects.get(
            session_id=scope.session, seq=1
        ).operation_id
    assert pending_operation is not None
    with runtime_role():
        assert execute_operation(pending_operation) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=2,
        )
        if finalized:
            saved = finalize_transcript(
                clinic_id=graph.clinic_a,
                session_id=scope.session,
                expected_revision=saved.revision,
                acknowledge_incomplete=True,
            )
        assert (
            ingest_chunk(
                clinic_id=graph.clinic_a,
                encounter_token=scope.token,
                chunk=chunk(scope, seq=2),
            ).code
            == "session_not_recording"
        )
        request = RequestFactory().post(
            "/",
            {
                "action": "refresh",
                "encounter_id": str(scope.encounter),
                "session_id": str(scope.session),
            },
        )
        request.session = SessionStore()
        request.session.update(
            {
                f"ehr.encounter.{graph.clinic_a}": str(scope.encounter),
                f"{SESSION_PREFIX}{graph.clinic_a}": str(
                    RecordingSession.objects.get(pk=scope.session).enrollment_id
                ),
            }
        )
        request.session.create()
        response = inspect.unwrap(transcript_review)(request, graph.clinic_a)
        assert response.status_code == 200
        assert b'id="transcript-coverage-title"' in response.content
        current = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
        assert current is not None
        assert current.revision == saved.revision
        assert current.segments == saved.segments
        assert (
            TranscriptVersion.objects.filter(session_id=scope.session).count()
            == saved.revision
        )
        if finalized:
            assert current == saved
            assert b'data-transcript-state="final"' in response.content
        else:
            assert [slot.sequence for slot in current.missing] == [1, 2]
            assert current.missing[0].chunk_id is not None
            assert current.missing[1].chunk_id is None


def test_coverage_reader_rechecks_native_assignment_and_active_actor(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
        recording = RecordingSession.objects.get(pk=scope.session)
        expected = coverage_for(recording)
        assert [slot.sequence for slot in expected] == [0, 1]
        assert all(slot.chunk_id is None for slot in expected)
    try:
        for actor, organization, active, allowed in (
            (graph.physician, graph.organization_a, True, True),
            (graph.shared_user, graph.organization_a, True, False),
            (graph.shared_user, graph.organization_b, True, False),
            (graph.physician, graph.organization_a, False, False),
            (graph.physician, graph.organization_a, True, True),
        ):
            # Only trusted fixture setup writes the actor; the callable runs
            # under the unchanged runtime grants and raw, untrusted GUCs.
            User.objects.filter(pk=graph.physician).update(is_active=active)
            with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_tenant',%s,true),"
                    "set_config('app.current_user_id',%s,true)",
                    [str(organization), str(actor)],
                )
                assert coverage_for(recording) == (expected if allowed else ())
                assert coverage_for(RecordingSession(pk=uuid4())) == ()
    finally:
        User.objects.filter(pk=graph.physician).update(is_active=True)


def test_finalize_without_full_coverage_requires_explicit_disposition(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
        with pytest.raises(TranscriptError, match="coverage_incomplete"):
            finalize_transcript(
                clinic_id=graph.clinic_a,
                session_id=scope.session,
                expected_revision=0,
                acknowledge_incomplete=False,
            )
        final = finalize_transcript(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=0,
            acknowledge_incomplete=True,
        )
        assert final.incomplete_acknowledged
        assert len(final.missing) == 2


def test_ai_revocation_refuses_an_already_queued_invocation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    acceptance = permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        request = ChunkTranscription.objects.select_related("invocation").get(
            session_id=scope.session,
        )
        inference_operation = request.invocation.operation_id
    with runtime_role(), patient_session_context(scope.patient_session):
        revoke_consent(acceptance_id=acceptance)
    assert inference_operation is not None
    with runtime_role():
        assert execute_operation(inference_operation) == "cancelled"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not TranscriptVersion.objects.filter(session_id=scope.session).exists()
        assert AudioChunk.objects.get(session_id=scope.session).wrapped_key


def test_empty_coverage_requires_acknowledgment_even_for_direct_sql(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=-1,
        )
        with (
            pytest.raises(DatabaseError, match="transcript coverage incomplete"),
            transaction.atomic(),
        ):
            TranscriptVersion.objects.create(
                organization_id=graph.organization_a,
                session_id=scope.session,
                revision=1,
                state="final",
                mutation="finalized",
                coverage={
                    "slots": [],
                    "completed": [],
                    "sources": [],
                    "recording_closed": True,
                },
                created_by_id=graph.physician,
            )
        assert not TranscriptVersion.objects.filter(session_id=scope.session).exists()
        final = finalize_transcript(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=0,
            acknowledge_incomplete=True,
        )
        assert final.state == "final"
        assert final.incomplete_acknowledged
        assert final.segments == ()


def test_asr_outage_retains_audio_and_never_fabricates_transcript_text(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        request = ChunkTranscription.objects.select_related("invocation").get(
            session_id=scope.session,
        )
        inference_operation = request.invocation.operation_id

    def unavailable(
        self: ScribeSyntheticProvider, *, request: ProviderRequest
    ) -> object:
        raise ProviderUnavailableError

    monkeypatch.setattr(
        "apps.scribe.gateway.ScribeSyntheticProvider.transcribe", unavailable
    )
    assert inference_operation is not None
    with runtime_role():
        assert execute_operation(inference_operation) == "failed"
        assert run_transcription(operation_id=str(operation)) == "transcription_pending"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not TranscriptVersion.objects.filter(session_id=scope.session).exists()
        assert AudioChunk.objects.get(session_id=scope.session).wrapped_key
