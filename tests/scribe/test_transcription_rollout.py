"""Native ASR provenance keeps the request's original invocation permission."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.effects import check_invocation
from apps.core.integration import execute_operation
from apps.scribe.models import ChunkTranscription, TranscriptSegment, TranscriptVersion
from apps.scribe.services import (
    control_recording,
    finalize_transcript,
    read_transcript,
    review_segment,
)
from apps.scribe.tasks import run_transcription
from apps.scribe.transcript_contracts import SegmentReview
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from ai.retained_rollout_support import (
    INVALIDATIONS,
    Invalidation,
    authorize_native,
    invalidate,
)
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scribe.test_ingest import media, world
from scribe.test_transcript_runtime import accepted, admit_interactive_work, permit_asr

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph
    from scribe.test_ingest import World

__all__ = ("admit_interactive_work", "media")
pytestmark = pytest.mark.django_db(transaction=True)
ASR_INVALIDATIONS = tuple(mode for mode in INVALIDATIONS if mode != "foreign_clinic")
REFUSED = "transcription invocation permission required"


@pytest.fixture
def recognized(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> tuple[World, ChunkTranscription]:
    scope = world(rbac_graph)
    permit_asr(rbac_graph, scope, monkeypatch)
    operation = accepted(rbac_graph, scope)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "queued"
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        request = ChunkTranscription.objects.select_related("invocation").get(
            session_id=scope.session
        )
        inference = request.invocation.operation_id
    assert inference is not None
    with runtime_role():
        assert execute_operation(inference) == "succeeded"
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        request = ChunkTranscription.objects.select_related("invocation").get(
            pk=request.pk
        )
        assert request.invocation.status == "succeeded"
        assert set(
            TranscriptVersion.objects.filter(request=request).values_list(
                "recognition_index", flat=True
            )
        ) == {0, 1}
    return scope, request


def insert_version(
    request: ChunkTranscription, *, write_transaction_id: int = 0
) -> TranscriptVersion:
    prior = TranscriptVersion.objects.filter(session_id=request.session_id).latest(
        "revision"
    )
    used = set(
        TranscriptVersion.objects.filter(request=request).values_list(
            "recognition_index", flat=True
        )
    )
    return TranscriptVersion.objects.create(
        organization_id=request.organization_id,
        session_id=request.session_id,
        predecessor=prior,
        revision=prior.revision + 1,
        state="partial",
        mutation="recognized",
        coverage=prior.coverage,
        request=request,
        recognition_index=min(set(range(4)) - used),
        created_by_id=prior.created_by_id,
        write_transaction_id=write_transaction_id,
    )


def insert_segment(
    request: ChunkTranscription, version: TranscriptVersion
) -> TranscriptSegment:
    return TranscriptSegment.objects.create(
        organization_id=request.organization_id,
        session_id=request.session_id,
        version=version,
        source_id=version.coverage["sources"][0],
        chunk_id=request.chunk_id,
        start_ms=0,
        end_ms=1000,
        text="Synthetic native recognition fixture",
        speaker="unknown",
        confidence_milli=900,
        uncertainty=[],
        language="pt-BR",
    )


def counts(request: ChunkTranscription) -> tuple[int, int]:
    return (
        TranscriptVersion.objects.filter(session_id=request.session_id).count(),
        TranscriptSegment.objects.filter(session_id=request.session_id).count(),
    )


def version_probe(
    request: ChunkTranscription, *, evidence: bool, refused: bool
) -> None:
    """Both writes use runtime grants and the actual, enabled transcript guard."""
    with (
        runtime_role(),
        tenant_context(request.invocation.user_id, request.organization_id),
    ):
        before = counts(request)
        if evidence:
            assert (
                authorize_native(
                    invocation_id=request.invocation_id,
                    clinic_id=request.invocation.clinic_id,
                    purpose=Purpose.SCRIBE_ASR,
                )
                is not refused
            )
        if refused:
            with pytest.raises(DatabaseError, match=REFUSED), transaction.atomic():
                insert_segment(request, insert_version(request))
        else:
            with transaction.atomic():
                insert_segment(request, insert_version(request))
                assert counts(request) == (before[0] + 1, before[1] + 1)
                transaction.set_rollback(True)
        assert counts(request) == before


def segment_probe(
    request: ChunkTranscription, *, evidence: bool, refused: bool
) -> None:
    """Owner seeds only the parent; the target segment's guard is never disabled."""
    with owner_context(request.organization_id), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true),txid_current()",
            [str(request.invocation.user_id)],
        )
        xid = cursor.fetchone()[1]
        cursor.execute(
            "ALTER TABLE clinic_app.scribe_transcriptversion "
            "DISABLE TRIGGER transcript_binding"
        )
        try:
            with transaction.atomic():
                version = insert_version(request, write_transaction_id=xid)
        finally:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(
                "ALTER TABLE clinic_app.scribe_transcriptversion "
                "ENABLE TRIGGER transcript_binding"
            )
        with runtime_role():
            before = counts(request)
            if evidence:
                assert (
                    authorize_native(
                        invocation_id=request.invocation_id,
                        clinic_id=request.invocation.clinic_id,
                        purpose=Purpose.SCRIBE_ASR,
                    )
                    is not refused
                )
            if refused:
                with pytest.raises(DatabaseError, match=REFUSED), transaction.atomic():
                    insert_segment(request, version)
                assert counts(request) == before
            else:
                insert_segment(request, version)
                assert counts(request) == (before[0], before[1] + 1)
        transaction.set_rollback(True)


@pytest.mark.parametrize("mode", ASR_INVALIDATIONS)
def test_native_recognition_refuses_invalidated_original_invocation(
    recognized: tuple[World, ChunkTranscription],
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
) -> None:
    _scope, request = recognized
    version_probe(request, evidence=True, refused=False)
    clinic = invalidate(rbac_graph, request.invocation_id, mode, monkeypatch)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        pytest.raises((GatewayAccessDeniedError, GatewayRefusedError)),
    ):
        check_invocation(clinic_id=clinic, invocation_id=request.invocation_id)
    version_probe(request, evidence=True, refused=True)


@pytest.mark.parametrize("mode", ASR_INVALIDATIONS)
def test_native_segments_independently_require_current_original_invocation(
    recognized: tuple[World, ChunkTranscription],
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
) -> None:
    _scope, request = recognized
    segment_probe(request, evidence=True, refused=False)
    invalidate(rbac_graph, request.invocation_id, mode, monkeypatch)
    segment_probe(request, evidence=True, refused=True)


def test_native_recognition_cannot_reuse_a_prior_transactions_evidence(
    recognized: tuple[World, ChunkTranscription],
) -> None:
    _scope, request = recognized
    version_probe(request, evidence=True, refused=False)
    version_probe(request, evidence=False, refused=True)


def test_native_segments_cannot_reuse_a_prior_transactions_evidence(
    recognized: tuple[World, ChunkTranscription],
) -> None:
    _scope, request = recognized
    segment_probe(request, evidence=True, refused=False)
    segment_probe(request, evidence=False, refused=True)


@pytest.mark.parametrize("mode", ASR_INVALIDATIONS)
def test_manual_correction_relabel_and_finalization_survive_ai_invalidation(
    recognized: tuple[World, ChunkTranscription],
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
) -> None:
    scope, request = recognized
    invalidate(rbac_graph, request.invocation_id, mode, monkeypatch)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        current = read_transcript(
            clinic_id=rbac_graph.clinic_a, session_id=scope.session
        )
        assert current is not None
        corrected = review_segment(
            clinic_id=rbac_graph.clinic_a,
            session_id=scope.session,
            expected_revision=current.revision,
            review=SegmentReview(
                current.segments[0].segment_id,
                text="Synthetic clinician correction",
                uncertainty=(),
            ),
        )
        relabeled = review_segment(
            clinic_id=rbac_graph.clinic_a,
            session_id=scope.session,
            expected_revision=corrected.revision,
            review=SegmentReview(corrected.segments[0].segment_id, speaker="caregiver"),
        )
        control_recording(
            clinic_id=rbac_graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=0,
        )
        final = finalize_transcript(
            clinic_id=rbac_graph.clinic_a,
            session_id=scope.session,
            expected_revision=relabeled.revision,
            acknowledge_incomplete=False,
        )
        assert final.state == "final"
        assert final.segments[0].text == "Synthetic clinician correction"
        assert final.segments[0].speaker == "caregiver"
