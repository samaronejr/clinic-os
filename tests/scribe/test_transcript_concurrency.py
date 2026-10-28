"""Native acceptance/settlement serialization and bounded failure outcomes."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from apps.ai.models import AIArtifact, AIInvocation
from apps.consent.services import publish_text
from apps.core.integration import execute_operation
from apps.scribe.models import AudioChunk, RecordingSession, TranscriptVersion
from apps.scribe.services import control_recording, finalize_transcript, read_transcript
from apps.scribe.tasks import run_transcription
from apps.scribe.transcript_contracts import TranscriptError, TranscriptReason
from apps.scribe.transcripts import (
    TranscriptAccessDeniedError,
    current_snapshot,
    save_snapshot,
)
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection, connections, transaction
from psycopg import Error as PsycopgError

from patient_service_support import runtime_role
from scribe.test_ingest import chunk, media, receive, world
from scribe.test_transcript_runtime import accepted, admit_interactive_work, permit_asr

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.scribe.transcript_contracts import TranscriptVersionDTO
    from apps.scribe.transcripts import Mutation, RecognitionReceipt

    from rbac_fixtures import RbacGraph

__all__ = ("admit_interactive_work", "media")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("purpose", "fenced"),
    [
        ("ai_assistance", True),
        ("consultation_recording", True),
        ("teleconsultation", False),
    ],
)
def test_native_consent_publication_holds_asr_send_fence(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    purpose: str,
    fenced: bool,
) -> None:
    graph = rbac_graph
    world(graph)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        publish_text(
            clinic_id=graph.clinic_a,
            purpose=purpose,
            text="SINTETICO: novo consentimento para a prova de isolamento.",
        )
        # Publication has returned, but its tenant transaction remains open.
        # Only the native trigger takes the AI send fence. A deleted function
        # or no-op body lets this independent, nonblocking contender acquire it.
        with psycopg.connect(superuser_database_url) as contender:
            assert contender.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                [
                    "clinic-lock-v1:comms-subject:ai-organization:"
                    f"{graph.organization_a}"
                ],
            ).fetchone() == (not fenced,)


@pytest.mark.parametrize("action", ["settle", "finalize"])
def test_media_acceptance_serializes_with_transcript_snapshot(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    graph, scope = rbac_graph, world(rbac_graph)
    permit_asr(graph, scope, monkeypatch)
    first = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(first)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        inference = AIInvocation.objects.get().operation_id
        assert receive(graph, scope, chunk(scope, seq=1)) == "received"
        media_operation = AudioChunk.objects.get(seq=1).operation_id
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
    assert inference is not None
    assert media_operation is not None
    coverage_read, acceptance_attempted = Event(), Event()

    def pause_snapshot(
        recording: RecordingSession,
        value: TranscriptVersionDTO,
        *,
        mutation: Mutation,
        recognition: RecognitionReceipt | None = None,
        changed_source: UUID | None = None,
    ) -> TranscriptVersionDTO:
        if not coverage_read.is_set():
            coverage_read.set()
            assert acceptance_attempted.wait(timeout=30)
        return save_snapshot(
            recording,
            value,
            mutation=mutation,
            recognition=recognition,
            changed_source=changed_source,
        )

    def observe(
        execute: Callable[..., object],
        query: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if "scribe_recordingsession" in query.lower() and "FOR UPDATE" in query:
            acceptance_attempted.set()
        return execute(query, params, many, context)

    def accept_media() -> str:
        connections.close_all()
        try:
            assert coverage_read.wait(timeout=30)
            with runtime_role(), connection.execute_wrapper(observe):
                return execute_operation(media_operation)
        finally:
            # With the former missing session lock this signals only after
            # acceptance committed, deterministically invalidating the read.
            acceptance_attempted.set()
            connections.close_all()

    target = (
        "apps.scribe.transcription.save_snapshot"
        if action == "settle"
        else "apps.scribe.transcripts.save_snapshot"
    )
    monkeypatch.setattr(target, pause_snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(accept_media)
        try:
            if action == "settle":
                with runtime_role():
                    assert execute_operation(inference) == "succeeded"
            else:
                with (
                    runtime_role(),
                    tenant_context(graph.physician, graph.organization_a),
                ):
                    assert (
                        finalize_transcript(
                            clinic_id=graph.clinic_a,
                            session_id=scope.session,
                            expected_revision=0,
                            acknowledge_incomplete=True,
                        ).state
                        == "final"
                    )
        finally:
            coverage_read.set()
        assert future.result(timeout=30) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert AudioChunk.objects.get(seq=1).state == "accepted"
        current = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
        assert current is not None
        assert current.state == ("partial" if action == "settle" else "final")
        assert len(current.missing) == (1 if action == "settle" else 2)


def test_database_coverage_change_is_a_recoverable_transcript_conflict(
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
        stale = replace(
            current_snapshot(recording),
            revision=1,
            state="final",
            coverage=(),
            incomplete_acknowledged=True,
        )
        with pytest.raises(TranscriptError) as error:
            save_snapshot(recording, stale, mutation="finalized")
        assert error.value.code == TranscriptReason.STALE
        assert not TranscriptVersion.objects.exists()
        assert (
            finalize_transcript(
                clinic_id=graph.clinic_a,
                session_id=scope.session,
                expected_revision=0,
                acknowledge_incomplete=True,
            ).state
            == "final"
        )


def test_timeline_overflow_uses_the_shared_transient_failure_path(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope, sequence=1)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        inference = AIInvocation.objects.get().operation_id
    # Chunk-local output fits, but its actual sequence offset crosses the
    # limit during projection, exactly as a recovered late epoch would.
    monkeypatch.setattr("apps.scribe.transcript_contracts.MAX_TIMELINE_MS", 2000)
    assert inference is not None
    with runtime_role():
        assert execute_operation(inference) == "retry"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert AIInvocation.objects.get().status == "reserved"
        assert not AIArtifact.objects.filter(kind="output").exists()
        assert not TranscriptVersion.objects.exists()
        assert AudioChunk.objects.get().wrapped_key


def test_prepare_authority_loss_is_committed_without_a_provider_call(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        inference = AIInvocation.objects.get().operation_id
    assert inference is not None
    with (
        patch(
            "apps.scribe.gateway.materialize_input",
            side_effect=TranscriptAccessDeniedError,
        ),
        patch("apps.scribe.gateway.ScribeSyntheticProvider.transcribe") as provider,
        runtime_role(),
    ):
        assert execute_operation(inference) == "cancelled"
    provider.assert_not_called()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        invocation = AIInvocation.objects.get()
        assert (invocation.status, invocation.reason_code) == (
            "failed",
            "authority_revoked",
        )
        assert not AIArtifact.objects.filter(kind="output").exists()
        assert AudioChunk.objects.get().wrapped_key


def test_native_coverage_storage_has_per_version_and_recording_bounds(
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
        coverage = {
            "slots": [],
            "completed": [],
            "sources": [str(uuid4())] * 27_000,
            "recording_closed": True,
        }

        def append(
            revision: int, predecessor: TranscriptVersion | None
        ) -> TranscriptVersion:
            return TranscriptVersion.objects.create(
                organization_id=graph.organization_a,
                session_id=scope.session,
                predecessor=predecessor,
                revision=revision,
                state="final",
                mutation="finalized" if predecessor is None else "corrected",
                coverage=coverage,
                incomplete_acknowledged=True,
                created_by_id=graph.physician,
            )

        with pytest.raises(IntegrityError) as oversized, transaction.atomic():
            append(1, None)
        assert isinstance(oversized.value.__cause__, PsycopgError)
        assert (
            oversized.value.__cause__.diag.constraint_name
            == "scribe_transcript_capacity"
        )
        coverage["sources"] = [str(uuid4())] * 26_000
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT %s / octet_length(%s::jsonb::text)",
                [64 * 1024 * 1024, json.dumps(coverage)],
            )
            permitted = int(cursor.fetchone()[0])
        previous = None
        for revision in range(1, permitted + 1):
            with transaction.atomic():
                previous = append(revision, previous)
        with pytest.raises(IntegrityError) as full, transaction.atomic():
            append(permitted + 1, previous)
        assert isinstance(full.value.__cause__, PsycopgError)
        assert full.value.__cause__.diag.constraint_name == "scribe_transcript_capacity"
        assert TranscriptVersion.objects.count() == permitted
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sum(octet_length(coverage::text)) "
                "FROM clinic_app.scribe_transcriptversion"
            )
            assert cursor.fetchone()[0] <= 64 * 1024 * 1024
