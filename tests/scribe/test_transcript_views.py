"""Native transcript review runs through login, OTP, middleware and real RLS."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core.integration import execute_operation
from apps.ehr.services import open_unscheduled_encounter
from apps.scribe.models import ChunkTranscription, RecordingSession, TranscriptVersion
from apps.scribe.services import control_recording, read_transcript
from apps.scribe.tasks import run_transcription
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.urls import reverse

from core.test_workspace_boundaries import _normalized_denial
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.test_ingest import World, media, world
from scribe.test_refusals import API, freeze_successful_session_activity, snapshot
from scribe.test_transcript_runtime import accepted, permit_asr

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def interactive_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.scribe.tasks.fair_acquire", lambda **_kwargs: True)


def bind(
    graph: RbacGraph,
    scope: World,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = client.session
    with tenant_context(graph.physician, graph.organization_a):
        recording = RecordingSession.objects.get(pk=scope.session)
    session[f"workspace.patient.{graph.clinic_a}"] = str(recording.enrollment_id)
    session[f"ehr.encounter.{graph.clinic_a}"] = str(scope.encounter)
    session.save()
    freeze_successful_session_activity(API(graph, scope, client), monkeypatch)


def post(
    client: Client,
    graph: RbacGraph,
    scope: World,
    action: str,
    **changes: object,
) -> _MonkeyPatchedWSGIResponse:
    values: Mapping[str, object] = {
        "encounter_id": str(scope.encounter),
        "session_id": str(scope.session),
        "action": action,
        **changes,
    }
    return client.post(reverse("scribe:transcript", args=(graph.clinic_a,)), values)


def other_encounter(graph: RbacGraph, scope: World) -> UUID:
    """Use a real second assignment for the same patient, not forged ORM state."""
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        enrollment = RecordingSession.objects.get(pk=scope.session).enrollment_id
        return open_unscheduled_encounter(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment,
            reason="documentation_only",
        ).pk


@pytest.mark.parametrize("htmx", [False, True], ids=["native", "htmx"])
def test_recording_from_another_authorized_encounter_has_unknown_denial_parity(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    htmx: bool,
) -> None:
    graph = rbac_graph
    original = world(graph)
    selected = replace(original, encounter=other_encounter(graph, original))
    assert selected.encounter != original.encounter
    with physician_client(graph) as client:
        bind(graph, selected, client, monkeypatch)
        # Both encounters are assigned and readable; only their pairing is bad.
        assert post(client, graph, selected, "open", session_id="").status_code == 200
        before = snapshot()
        session = dict(client.session)
        responses = []
        for identifier in (original.session, uuid4()):
            response = client.post(
                reverse("scribe:transcript", args=(graph.clinic_a,)),
                {
                    "encounter_id": str(selected.encounter),
                    "session_id": str(identifier),
                    "action": "open",
                },
                HTTP_HX_REQUEST="true" if htmx else "false",
            )
            assert response.status_code == 403
            assert b"data-transcript-denied" in response.content
            assert not response.wsgi_request.session.modified
            assert settings.SESSION_COOKIE_NAME not in response.cookies
            assert dict(client.session) == session
            assert snapshot() == before
            responses.append(response)
        assert _normalized_denial(responses[0]) == _normalized_denial(responses[1])


def test_native_speaker_correction_finalization_and_revision_conflicts(
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
        inference = ChunkTranscription.objects.get(
            session_id=scope.session
        ).invocation.operation_id
    assert inference is not None
    with runtime_role():
        assert execute_operation(inference) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        current = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
        assert current is not None
        segment = current.segments[0].segment_id
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=0,
        )
    with physician_client(graph) as client:
        bind(graph, scope, client, monkeypatch)
        opened = post(client, graph, scope, "open", session_id="")
        assert opened.status_code == 200
        assert b"data-transcript-segment" in opened.content
        relabeled = post(
            client,
            graph,
            scope,
            "relabel",
            expected_revision=2,
            segment_id=str(segment),
            speaker="caregiver",
        )
        assert relabeled.status_code == 200
        stale = post(
            client,
            graph,
            scope,
            "relabel",
            expected_revision=2,
            segment_id=str(segment),
            speaker="patient",
        )
        assert stale.status_code == 409
        corrected = post(
            client,
            graph,
            scope,
            "correct",
            expected_revision=3,
            segment_id=str(segment),
            text="SINTETICO-41: texto revisado <script>sem execução</script>",
        )
        assert corrected.status_code == 200
        assert b"<script>sem" not in corrected.content
        invalid_review = post(
            client,
            graph,
            scope,
            "relabel",
            expected_revision=4,
        )
        assert invalid_review.status_code == 409
        finalized = post(client, graph, scope, "finalize", expected_revision=4)
        assert finalized.status_code == 200
        assert b'data-transcript-state="final"' in finalized.content
        assert (
            post(client, graph, scope, "finalize", expected_revision=5).status_code
            == 409
        )
        with tenant_context(graph.physician, graph.organization_a):
            final = read_transcript(clinic_id=graph.clinic_a, session_id=scope.session)
            assert final is not None
            assert final.revision == 5
            assert final.segments[0].speaker == "caregiver"
            assert final.segments[0].text_reviewed
            assert final.segments[0].uncertainty == ()
            assert (
                TranscriptVersion.objects.filter(session_id=scope.session).count() == 5
            )


def test_queue_publishes_only_unrequested_audio_after_the_request_commit(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    sent: list[object] = []
    monkeypatch.setattr(
        "apps.scribe.transcript_views.dispatch_transcription", sent.append
    )
    with physician_client(graph) as client:
        bind(graph, scope, client, monkeypatch)
        assert post(client, graph, scope, "queue").status_code == 200
        assert len(sent) == 1
        assert run_transcription(operation_id=str(operation)) == "queued"
        assert post(client, graph, scope, "queue").status_code == 200
        assert len(sent) == 1


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"action": "unknown"}, 400),
        ({"expected_revision": "-1"}, 400),
        ({"session_id": str(uuid4())}, 403),
        ({"encounter_id": str(uuid4())}, 403),
        ({"speaker": "second_voice"}, 400),
    ],
)
def test_native_invalid_and_foreign_selectors_cannot_append_history(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, str],
    status: int,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with physician_client(graph) as client:
        bind(graph, scope, client, monkeypatch)
        values = {"action": "refresh", **changes}
        action = values.pop("action")
        assert post(client, graph, scope, action, **values).status_code == status
        with tenant_context(graph.physician, graph.organization_a):
            assert not TranscriptVersion.objects.filter(
                session_id=scope.session
            ).exists()
