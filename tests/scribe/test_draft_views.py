"""Native review keeps refused commands, manual edits and session state intact."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.contracts import Purpose
from apps.core import workspace
from apps.core.navigation import granted_permissions
from apps.ehr.finalization import finalize_version
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import create_draft, record_clinical_note
from apps.scribe.models import DraftSectionSuggestion, ReviewDecision
from apps.scribe.templatetags import scribe_review
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext

from ai.support import model
from auth.stepup_test_support import verified_request
from core.test_workspace_boundaries import _normalized_denial
from otp_test_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.draft_support import generate
from scribe.test_draft_runtime import accept, draft_world
from scribe.test_ingest import media
from scribe.test_refusals import snapshot
from scribe.test_transcript_views import bind, other_encounter

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


@pytest.mark.parametrize("htmx", [False, True], ids=["native", "htmx"])
def test_note_from_another_authorized_encounter_has_unknown_denial_parity(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
    htmx: bool,
) -> None:
    scope, graph = draft_world, draft_world.graph
    encounter = other_encounter(graph, scope.recording)
    assert encounter != scope.recording.encounter
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        original = ClinicalDocumentVersion.objects.get(pk=scope.version)
        version = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter,
            template_id=original.template_id,
        )
    selected = replace(
        scope,
        recording=replace(scope.recording, encounter=encounter),
        version=version.pk,
    )
    with physician_client(graph) as client:
        bind(graph, selected.recording, client, monkeypatch)
        assert post(client, selected).status_code == 200
        before = snapshot()
        session = dict(client.session)
        responses = []
        for identifier in (scope.version, uuid4()):
            response = client.post(
                reverse("scribe:review", args=(graph.clinic_a,)),
                {
                    "encounter_id": str(encounter),
                    "version_id": str(identifier),
                    "action": "open",
                },
                HTTP_HX_REQUEST="true" if htmx else "false",
            )
            assert response.status_code == 403
            assert b"data-review-denied" in response.content
            assert not response.wsgi_request.session.modified
            assert settings.SESSION_COOKIE_NAME not in response.cookies
            assert dict(client.session) == session
            assert snapshot() == before
            responses.append(response)
        assert _normalized_denial(responses[0]) == _normalized_denial(responses[1])


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


def test_finalized_chart_pages_through_distinct_reviewed_configurations(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, graph = draft_world, draft_world.graph
    clinic = graph.clinic_a
    origins = []
    # Exercise the production page boundary with two real, governed generations;
    # the companion 101-record projection test covers the shipped page size.
    monkeypatch.setattr(scribe_review, "HISTORY_PAGE_SIZE", 1)
    for generation in range(2):
        if generation:
            model(
                organization=graph.organization_a,
                clinic=clinic,
                purpose=Purpose.SCRIBE_DRAFT,
            )
        draft = generate(scope)
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            suggestion = DraftSectionSuggestion.objects.get(
                draft=draft, section="subjective"
            )
            origins.append(
                accept(scope, suggestion).capability_snapshot["configuration_id"]
            )
    assert origins[0] != origins[1]
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        recorded = record_clinical_note(
            clinic_id=clinic,
            version_id=version.pk,
            expected_revision=version.revision,
            content={key: value or "Sintético" for key, value in version.soap.items()},
        )
        finalized = finalize_version(
            clinic_id=clinic,
            version_id=version.pk,
            expected_revision=recorded.revision,
            request=request,
        )
        assert finalized.state == "finalized"
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        for index in (0, 1):
            response = client.post(
                reverse("ehr:encounter", args=(clinic,)),
                {
                    "action": "show",
                    "encounter_id": str(scope.recording.encounter),
                    "ai_use_page": str(index + 1),
                },
            )
            assert response.status_code == 200
            assert response.content.count(b"data-ai-use-record") == 1
            assert origins[1 - index].encode() in response.content
            assert origins[index].encode() not in response.content
            assert b'name="ai_use_page"' in response.content
            assert b'data-state="finalized"' in response.content


@pytest.mark.parametrize("admitted", [False, True], ids=["physician", "automation"])
def test_review_offers_the_proposal_queue_only_with_automation_authority(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
) -> None:
    scope, graph = draft_world, draft_world.graph
    generate(scope)
    if admitted:
        monkeypatch.setattr(
            workspace,
            "granted_permissions",
            lambda clinic_id: granted_permissions(clinic_id) | {"automation.admin"},
        )
    with physician_client(graph) as client:
        bind(graph, scope.recording, client, monkeypatch)
        opened = post(client, scope)
    assert opened.status_code == 200
    assert b"data-separate-proposals" in opened.content
    link = gettext("Open governed proposal queue") in opened.content.decode()
    assert link is admitted


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
