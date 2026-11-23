"""Unfinalized ASR text is not the clinician's authored medical record."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.consent.services import revoke_consent
from apps.core.integration import execute_operation
from apps.intake.patient_access import patient_session_context
from apps.scribe.models import ChunkTranscription
from apps.scribe.tasks import run_transcription
from apps.scribe.transcripts import read_transcript
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.test_ingest import world
from scribe.test_transcript_runtime import accepted, admit_interactive_work, permit_asr
from scribe.test_transcript_views import bind, post

if TYPE_CHECKING:
    import pytest

    from rbac_fixtures import RbacGraph


def partial_transcript_read(g: RbacGraph, patch: pytest.MonkeyPatch) -> None:
    admit_interactive_work._get_wrapped_function()(patch)
    visit = world(g)
    receipt = permit_asr(g, visit, patch)
    media = accepted(g, visit)
    with runtime_role():
        assert run_transcription(operation_id=str(media)) == "queued"
    with runtime_role(), tenant_context(g.physician, g.organization_a):
        operation = ChunkTranscription.objects.get(
            session_id=visit.session
        ).invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    with physician_client(g) as client:
        bind(g, visit, client, patch)
        opened = post(client, g, visit, "open")
        assert opened.status_code == 200
        assert b"data-transcript-segment" in opened.content
        with patient_session_context(visit.patient_session):
            revoke_consent(acceptance_id=receipt)
        closed = post(client, g, visit, "refresh")
        assert closed.status_code in (200, 403)
        assert b"data-transcript-segment" not in closed.content
        with tenant_context(g.physician, g.organization_a):
            partial = read_transcript(clinic_id=g.clinic_a, session_id=visit.session)
            assert partial is None or not partial.segments
