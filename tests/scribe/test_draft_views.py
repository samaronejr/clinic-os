"""Native review keeps refused commands, manual edits and session state intact."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import record_clinical_note
from apps.scribe.models import DraftSectionSuggestion, ReviewDecision
from apps.tenancy.db import tenant_context
from django.urls import reverse
from django.utils.translation import gettext

from otp_test_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.draft_support import generate
from scribe.test_draft_runtime import draft_world
from scribe.test_ingest import media
from scribe.test_refusals import snapshot
from scribe.test_transcript_views import bind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from scribe.draft_support import DraftWorld

__all__ = ("draft_world", "media")
pytestmark = pytest.mark.django_db(transaction=True)


def post(
    client: Client, scope: DraftWorld, *, action: str = "open", **changes: object
) -> _MonkeyPatchedWSGIResponse:
    values: Mapping[str, object] = {
        "encounter_id": str(scope.recording.encounter),
        "version_id": str(scope.version),
        "action": action,
        **changes,
    }
    return client.post(reverse("scribe:review", args=(scope.graph.clinic_a,)), values)


def test_native_review_shows_sources_diff_and_persistent_ai_origin(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        opened = post(client, scope)
        assert opened.status_code == 200
        assert gettext("Clinician AI review") in opened.content.decode()
        assert b'data-suggestion-state="pending"' in opened.content
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            suggestion = DraftSectionSuggestion.objects.get(
                draft=queued, section="subjective"
            )
            content = ClinicalDocumentVersion.objects.get(pk=scope.version).soap
        accepted = post(
            client,
            scope,
            action="",
            decision=f"accepted:{suggestion.pk}",
            command_id=str(uuid4()),
            expected_revision=1,
            **content,
        )
        assert accepted.status_code == 200
        assert b"data-ai-use-record" in accepted.content
        assert b'data-provenance="ai_drafted"' in accepted.content
        assert accepted.headers["Cache-Control"].find("no-store") >= 0
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            assert ReviewDecision.objects.filter(version_id=scope.version).count() == 1
            assert (
                ClinicalDocumentVersion.objects.get(pk=scope.version).state == "draft"
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"version_id": str(uuid4())},
        {"encounter_id": str(uuid4())},
        {
            "action": "save",
            "expected_revision": 999,
            "subjective": "Sintetico retained",
        },
        {
            "action": "generate",
            "expected_revision": 1,
            "request_key": str(uuid4()),
            "transcript_id": str(uuid4()),
        },
    ],
)
def test_refused_http_commands_change_no_table_or_session(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
) -> None:
    scope, graph = draft_world, draft_world.graph
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        before = snapshot()
        session = dict(client.session)
        response = client.post(
            reverse("scribe:review", args=(graph.clinic_a,)),
            {
                "encounter_id": str(scope.recording.encounter),
                "version_id": str(scope.version),
                "action": "open",
                **changes,
            },
        )
        assert response.status_code in {403, 409}
        assert snapshot() == before
        assert dict(client.session) == session


def test_rejecting_a_suggestion_preserves_unsaved_manual_text_on_the_page(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
        content = ClinicalDocumentVersion.objects.get(pk=scope.version).soap
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        response = post(
            client,
            scope,
            action="",
            decision=f"rejected:{suggestion.pk}",
            command_id=str(uuid4()),
            expected_revision=1,
            **{**content, "subjective": "Sintetico unsaved manual words."},
        )
        assert response.status_code == 200
        assert b"Sintetico unsaved manual words." in response.content
        assert b'data-review-note-state="unsaved"' in response.content
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            assert (
                ClinicalDocumentVersion.objects.get(pk=scope.version).subjective == ""
            )
            assert (
                ReviewDecision.objects.get(suggestion=suggestion).applied_text is None
            )


def test_modified_review_requires_replacement_before_writing(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
        content = ClinicalDocumentVersion.objects.get(pk=scope.version).soap
    command = uuid4()
    replacement = "  SINTETICO-42: clinician replacement.  "
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        before = snapshot()
        session = dict(client.session)
        refused = post(
            client,
            scope,
            action="",
            decision=f"modified:{suggestion.pk}",
            command_id=str(command),
            expected_revision=1,
            **content,
        )
        assert refused.status_code == 400
        assert snapshot() == before
        assert dict(client.session) == session
        applied = post(
            client,
            scope,
            action="",
            decision=f"modified:{suggestion.pk}",
            command_id=str(command),
            expected_revision=1,
            **{**content, f"replacement-{suggestion.pk}": replacement},
        )
        assert applied.status_code == 200
        assert b'data-provenance="ai_drafted"' in applied.content
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            version = ClinicalDocumentVersion.objects.get(pk=scope.version)
            decision = ReviewDecision.objects.get(suggestion=suggestion)
            assert version.subjective == replacement
            assert version.revision == 2
            assert version.state == "draft"
            assert decision.action == "modified"
            assert decision.command_id == command
            assert decision.applied_text == replacement


def test_refresh_keeps_unsaved_text_without_rebasing_a_stale_editor(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, graph = draft_world, draft_world.graph
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        values = {
            "expected_revision": 1,
            "subjective": "Sintetico unsaved before refresh.",
        }
        response = post(client, scope, action="refresh", **values)
        assert response.status_code == 200
        assert b"Sintetico unsaved before refresh." in response.content
        assert b'data-review-note-state="unsaved"' in response.content
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            version = ClinicalDocumentVersion.objects.get(pk=scope.version)
            assert version.subjective == ""
            record_clinical_note(
                clinic_id=graph.clinic_a,
                version_id=scope.version,
                expected_revision=1,
                content={**version.soap, "subjective": "Sintetico other editor."},
            )
        before = snapshot()
        response = post(client, scope, action="refresh", **values)
        assert response.status_code == 409
        assert b"Sintetico unsaved before refresh." in response.content
        assert snapshot() == before
