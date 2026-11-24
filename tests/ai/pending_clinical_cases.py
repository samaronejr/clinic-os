"""Genuine visit, scribe and document cases for the read-category matrix."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from apps.ai.contracts import Purpose
from apps.ai.models import AIArtifact, AIInvocation, ExtractionCandidateRecord
from apps.core.integration import execute_operation
from apps.scribe.models import (
    ChunkTranscription,
    DraftSectionSuggestion,
    TranscriptSegment,
)
from apps.scribe.tasks import run_transcription
from django.urls import reverse

from ai.chart_consent_support import accept_ai
from ai.decision_support_helpers import enable, world_for
from ai.pending_display_support import withdraw
from ai.pending_reader_probe import ReaderCase
from ai.test_care_views import _client
from ai.test_extraction_runtime import extracted_job
from core.test_navigation import _post
from ehr.test_autosave import as_actor
from orders.support import ordered
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.draft_support import generate
from scribe.test_draft_views import post as draft_post
from scribe.test_ingest import world as recording_world
from scribe.test_transcript_runtime import accepted, admit_interactive_work, permit_asr
from scribe.test_transcript_views import bind
from scribe.test_transcript_views import post as asr_post

if TYPE_CHECKING:
    import pytest

    from rbac_fixtures import RbacGraph
    from scribe.draft_support import DraftWorld


def chart_case(g: RbacGraph, patch: pytest.MonkeyPatch, purpose: Purpose) -> ReaderCase:
    world = world_for(g, patch)
    enable(world)
    patient, receipt = accept_ai(g, world.chart.enrollment)
    enc = str(world.chart.version.document.encounter_id)
    brief = purpose is Purpose.BRIEF
    url = reverse(
        "ai:chart-assistant" if brief else "ai:decision-support", args=(g.clinic_a,)
    )
    client = _client(g.physician, "physician")
    selector = {"encounter_id": enc}
    if brief:
        selector["surface"] = "workspace"
    generated = _post(
        client,
        url,
        {**selector, "command": "generate", **({} if brief else {"opt_in": "yes"})},
    )
    assert generated.status_code == 200
    with as_actor(g, g.physician):
        invocation = AIInvocation.objects.get(
            purpose=purpose.value, user_id=g.physician
        )
        op = invocation.operation_id
    assert op is not None
    with runtime_role():
        assert execute_operation(operation_id=op) == "succeeded"
    with as_actor(g, g.physician):
        key = invocation.output.pk

    def visible(hx: bool) -> bool:
        client.defaults["HTTP_HX_REQUEST"] = "true" if hx else "false"
        read = _post(
            client,
            url,
            {**selector, "command": "display", "invocation_id": str(invocation.pk)},
        )
        assert read.status_code == 200
        marker = b'data-chart-state="ready"' if brief else b'data-cds-state="ready"'
        return marker in read.content

    return ReaderCase(
        AIArtifact._meta.db_table,
        frozenset({str(key)}),
        visible,
        partial(withdraw, patient, receipt),
    )


def extraction_case(g: RbacGraph, patch: pytest.MonkeyPatch) -> ReaderCase:
    source = ordered(g)
    patient, receipt = accept_ai(g, source.enrollment)
    job = extracted_job(g, patch, world=source)
    with as_actor(g, g.physician):
        keys = frozenset(str(k) for k in job.candidates.values_list("pk", flat=True))
    assert keys
    client = _client(g.physician, "physician")
    url = reverse("ai:extraction", args=(g.clinic_a,))

    def visible(hx: bool) -> bool:
        client.defaults["HTTP_HX_REQUEST"] = "true" if hx else "false"
        read = _post(client, url, {"action": "refresh", "job_id": str(job.pk)})
        assert read.status_code in (200, 403, 409)
        return any(k.encode() in read.content for k in keys)

    return ReaderCase(
        ExtractionCandidateRecord._meta.db_table,
        keys,
        visible,
        partial(withdraw, patient, receipt),
    )


def draft_case(scope: DraftWorld, patch: pytest.MonkeyPatch) -> ReaderCase:
    g = scope.graph
    draft = generate(scope)
    with as_actor(g, g.physician):
        key = DraftSectionSuggestion.objects.get(draft=draft, section="subjective").pk
    with physician_client(g) as client:
        bind(g, scope.recording, client, patch)

    def visible(hx: bool) -> bool:
        client.defaults["HTTP_HX_REQUEST"] = "true" if hx else "false"
        with runtime_role(), fixed_otp_time():
            read = draft_post(client, scope)
        assert read.status_code == 200
        return str(key).encode() in read.content

    return ReaderCase(
        DraftSectionSuggestion._meta.db_table,
        frozenset({str(key)}),
        visible,
        partial(withdraw, scope.recording.patient_session, draft.consent_id),
    )


def asr_case(g: RbacGraph, patch: pytest.MonkeyPatch) -> ReaderCase:
    admit_interactive_work._get_wrapped_function()(patch)
    visit = recording_world(g)
    receipt = permit_asr(g, visit, patch)
    media_op = accepted(g, visit)
    with runtime_role():
        assert run_transcription(operation_id=str(media_op)) == "queued"
    with as_actor(g, g.physician):
        op = ChunkTranscription.objects.get(
            session_id=visit.session
        ).invocation.operation_id
    assert op is not None
    with runtime_role():
        assert execute_operation(operation_id=op) == "succeeded"
    with as_actor(g, g.physician):
        keys = frozenset(
            str(k)
            for k in TranscriptSegment.objects.filter(
                session_id=visit.session
            ).values_list("pk", flat=True)
        )
    assert keys
    with physician_client(g) as client:
        bind(g, visit, client, patch)

    def visible(hx: bool) -> bool:
        client.defaults["HTTP_HX_REQUEST"] = "true" if hx else "false"
        with runtime_role(), fixed_otp_time():
            read = asr_post(client, g, visit, "refresh")
        assert read.status_code in (200, 403)
        return b"data-transcript-segment" in read.content

    return ReaderCase(
        TranscriptSegment._meta.db_table,
        keys,
        visible,
        partial(withdraw, visit.patient_session, receipt),
    )
