"""A crossed transcript/chunk reference is rejected before reading patient choice."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.models import AIArtifact
from apps.scribe import drafting, transcription
from apps.scribe.draft_contracts import DraftAccessDeniedError, DraftRequest
from apps.scribe.models import ChunkTranscription
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from scribe.test_draft_runtime import draft_world
from scribe.test_ingest import media

if TYPE_CHECKING:
    from scribe.draft_support import DraftWorld

__all__ = ("draft_world", "media")
pytestmark = pytest.mark.django_db(transaction=True)


def test_draft_checks_transcript_scope_before_ai_consent(
    draft_world: DraftWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = draft_world
    graph = scope.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # Both the note and live patient acceptance are valid; this transcript
        # does not belong to that visit. Never probe the patient's choice.
        def no_consent_read(*_args: object, **_kwargs: object) -> None:
            pytest.fail("out-of-scope transcript reached visit consent")

        monkeypatch.setattr(drafting, "drafting_consent", no_consent_read)
        with pytest.raises(DraftAccessDeniedError):
            drafting.request_draft(
                clinic_id=graph.clinic_a,
                version_id=scope.version,
                transcript_id=uuid4(),
                request_key=uuid4(),
                request=DraftRequest(1, ("subjective",)),
            )


def test_asr_checks_input_binding_before_recording_or_ai_consent(
    draft_world: DraftWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = draft_world
    graph = scope.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        transcription_row = ChunkTranscription.objects.filter(
            session_id=scope.recording.session
        ).first()
        assert transcription_row is not None
        actual = transcription_row.invocation.inputs_ref
        assert transcription.eligible_input(actual)
        # Real chunk reference, but a mismatched ephemeral artifact subject.
        # No database fixture or ledger row is forged or mutated.
        crossed = AIArtifact(
            clinic_id=actual.clinic_id,
            user_id=actual.user_id,
            patient_enrollment_id=uuid4(),
            content=actual.content,
        )

        def no_consent_read(*_args: object, **_kwargs: object) -> None:
            pytest.fail("out-of-scope audio input reached recording consent")

        monkeypatch.setattr(transcription, "transcription_consent", no_consent_read)
        assert not transcription.eligible_input(crossed)
