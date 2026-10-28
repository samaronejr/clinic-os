"""Real-role merge, generation, source, replay and immutable provenance proofs."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.models import AIArtifact, AIInvocation
from apps.core.fairness import DEFER_COUNTDOWN_SECONDS
from apps.core.integration import execute_operation
from apps.ehr.models import ClinicalDocumentVersion, DraftEditState
from apps.ehr.services import record_clinical_note
from apps.scribe.draft_contracts import (
    DraftAccessDeniedError,
    DraftConflictError,
    ReviewInput,
)
from apps.scribe.models import (
    AIDraft,
    ChunkTranscription,
    DraftSectionSuggestion,
    ReviewDecision,
)
from apps.scribe.services import (
    read_review,
    review_segment,
    review_suggestion,
)
from apps.scribe.tasks import run_transcription
from apps.scribe.transcript_contracts import SegmentReview
from apps.scribe.transcripts import read_transcript
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import reveal
from django.db import DatabaseError, connection, transaction
from psycopg import sql

from patient_service_support import runtime_role
from scribe.draft_support import DraftWorld, generate, queue, seed_draft_world
from scribe.test_drafting import encoded_draft
from scribe.test_ingest import media, world
from scribe.test_refusals import fixture_connection
from scribe.test_transcript_runtime import accepted, permit_asr

if TYPE_CHECKING:
    from apps.ai.contracts import ProviderRequest, ProviderResult
    from apps.scribe.gateway import ScribeSyntheticProvider

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


def audit_count() -> int:
    """Observe committed audit rows through the isolated fixture observer."""
    with fixture_connection() as observer:
        row = observer.execute("SELECT count(*) FROM clinic_app.audit_event").fetchone()
    assert row is not None
    count = row[0]
    assert isinstance(count, int)
    return count


@pytest.fixture
def draft_world(rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch) -> DraftWorld:
    return seed_draft_world(rbac_graph, monkeypatch)


def accept(scope: DraftWorld, suggestion: DraftSectionSuggestion) -> ReviewDecision:
    graph = scope.graph
    version = ClinicalDocumentVersion.objects.get(pk=scope.version)
    return review_suggestion(
        clinic_id=graph.clinic_a,
        version_id=scope.version,
        review=ReviewInput(
            suggestion.pk, version.revision, uuid4(), "accepted", version.soap
        ),
    )


def test_generation_and_three_clinician_decisions_never_finalize_automatically(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        assert version.revision == 1
        assert not any(version.soap.values())
        suggestions = tuple(
            DraftSectionSuggestion.objects.filter(draft=queued).order_by("section")
        )
        assert len(suggestions) == 3
        assert not any(item.section == "objective" for item in suggestions)
        for suggestion in suggestions:
            decision = accept(scope, suggestion)
            assert decision.action == "accepted"
            assert decision.capability_snapshot["purpose"] == "scribe_draft"
            assert decision.capability_snapshot["configuration_id"] == str(
                scope.configuration
            )
        version.refresh_from_db()
        assert version.revision == 4
        assert version.state == "draft"
        assert ReviewDecision.objects.filter(version=version).count() == 3
        assert all(
            row.state == "accepted"
            for row in read_review(
                clinic_id=graph.clinic_a, version_id=scope.version
            ).rows
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT base_content FROM clinic_app.scribe_aidraft WHERE id=%s",
                [queued.pk],
            )
            ciphertext = bytes(cursor.fetchone()[0])
            assert (
                json.loads(reveal(purpose="scribe.draft.base", envelope=ciphertext))
                == queued.base_content
            )


def test_late_result_keeps_manual_edits_and_requires_explicit_replacement(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = queue(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        content = {**version.soap, "subjective": "Sintetico manual - preserve."}
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=content,
        )
        operation = AIDraft.objects.get(pk=queued.pk).invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
    before = audit_count()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(DraftConflictError, match="section_conflict"):
            accept(scope, suggestion)
        version.refresh_from_db()
        assert version.subjective == content["subjective"]
        assert version.revision == 2
    assert audit_count() == before
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        decision = review_suggestion(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            review=ReviewInput(
                suggestion.pk,
                2,
                uuid4(),
                "modified",
                version.soap,
                "Sintetico clinician replacement.",
            ),
        )
        assert decision.action == "modified"
        version.refresh_from_db()
        assert version.subjective == "Sintetico clinician replacement."


def test_edit_then_revert_still_conflicts_and_explicit_deletion_is_a_clinician_edit(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        original = version.soap
        for value in ("Sintetico temporary", ""):
            version = record_clinical_note(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=version.revision,
                content={**original, "subjective": value},
            )
        state = DraftEditState.objects.get(version=version)
        assert state.section_edit_epochs["subjective"] == 2
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
        with pytest.raises(DraftConflictError, match="section_conflict"):
            accept(scope, suggestion)
        decision = review_suggestion(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            review=ReviewInput(
                suggestion.pk,
                version.revision,
                uuid4(),
                "modified",
                version.soap,
                "",
            ),
        )
        decision.refresh_from_db()
        assert decision.applied_text == ""
        assert decision.result_revision == version.revision


def test_section_regeneration_fences_late_results_without_invalidating_other_sections(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    first = queue(scope)
    second = queue(scope, sections=("subjective",))
    for draft in (first, second):
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            operation = AIDraft.objects.get(pk=draft.pk).invocation.operation_id
        assert operation is not None
        with runtime_role():
            assert execute_operation(operation) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not DraftSectionSuggestion.objects.filter(
            draft=first, section="subjective"
        ).exists()
        assert DraftSectionSuggestion.objects.filter(draft=first).count() == 2
        assert (
            DraftSectionSuggestion.objects.filter(
                draft=second, section="subjective"
            ).count()
            == 1
        )


def test_transcript_successor_marks_pending_suggestions_stale_without_note_mutation(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        transcript = read_transcript(
            clinic_id=graph.clinic_a, session_id=scope.recording.session
        )
        assert transcript is not None
        review_segment(
            clinic_id=graph.clinic_a,
            session_id=scope.recording.session,
            expected_revision=transcript.revision,
            review=SegmentReview(
                transcript.segments[0].segment_id,
                text="Sintetico corrected source.",
                uncertainty=(),
            ),
        )
        panel = read_review(clinic_id=graph.clinic_a, version_id=scope.version)
        assert all(row.state == "stale" for row in panel.rows)
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
    before = audit_count()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(DraftConflictError, match="source_superseded"):
            accept(scope, suggestion)
        assert ClinicalDocumentVersion.objects.get(pk=scope.version).revision == 1
    assert audit_count() == before


def test_exact_review_replay_is_read_only_and_changed_replay_is_refused(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
        command_id = uuid4()
        review = ReviewInput(suggestion.pk, 1, command_id, "accepted", version.soap)
        first = review_suggestion(
            clinic_id=graph.clinic_a, version_id=scope.version, review=review
        )
    before = audit_count()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert (
            review_suggestion(
                clinic_id=graph.clinic_a, version_id=scope.version, review=review
            ).pk
            == first.pk
        )
    assert audit_count() == before
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(DraftConflictError, match="idempotency_mismatch"),
    ):
        review_suggestion(
            clinic_id=graph.clinic_a,
            version_id=scope.version,
            review=replace(review, action="rejected"),
        )
    assert audit_count() == before
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        for table, identifier in (
            ("scribe_aidraft", queued.pk),
            ("scribe_draftsectionsuggestion", suggestion.pk),
            ("scribe_reviewdecision", first.pk),
        ):
            for command in ("UPDATE", "DELETE", "TRUNCATE"):
                statement = {
                    "UPDATE": sql.SQL("UPDATE {} SET id=id WHERE id=%s"),
                    "DELETE": sql.SQL("DELETE FROM {} WHERE id=%s"),
                    "TRUNCATE": sql.SQL("TRUNCATE {}"),
                }[command].format(sql.Identifier("clinic_app", table))
                parameters = [] if command == "TRUNCATE" else [identifier]
                with (
                    pytest.raises(DatabaseError),
                    transaction.atomic(),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(statement, parameters)


def test_other_actor_unknown_and_other_clinic_share_the_same_denial(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    generate(scope)
    for user, clinic, version in (
        (graph.physician, graph.clinic_a, uuid4()),
        (graph.physician, graph.clinic_b, scope.version),
        (graph.shared_user, graph.clinic_a, scope.version),
    ):
        before = audit_count()
        with (
            runtime_role(),
            tenant_context(user, graph.organization_a),
            pytest.raises(
                DraftAccessDeniedError, match="note suggestion access denied"
            ),
        ):
            read_review(clinic_id=clinic, version_id=version)
        assert audit_count() == before


@pytest.mark.parametrize("encoding", ["compact", "pretty", "reordered", "unicode"])
@pytest.mark.parametrize("attack", ["fabricated_citation", "forbidden_assertion"])
def test_decoded_provider_result_is_rejected_before_any_projection(
    draft_world: DraftWorld,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    attack: str,
) -> None:
    from apps.scribe.gateway import ScribeSyntheticProvider  # noqa: PLC0415

    scope, graph = draft_world, draft_world.graph
    original = ScribeSyntheticProvider.complete

    def corrupt(
        self: ScribeSyntheticProvider, *, request: ProviderRequest
    ) -> ProviderResult:
        result = original(self, request=request)
        data = json.loads(result.content)
        if attack == "fabricated_citation":
            data["suggestions"][0]["citations"][0]["id"] = str(uuid4())
        else:
            data["suggestions"][0]["text"] = "No symptoms."
        return replace(result, content=encoded_draft(json.dumps(data), encoding))

    monkeypatch.setattr("apps.scribe.gateway.ScribeSyntheticProvider.complete", corrupt)
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        assert (invocation.status, invocation.reason_code) == (
            "failed",
            "invalid_output",
        )
        assert not DraftSectionSuggestion.objects.filter(draft=queued).exists()
        assert ClinicalDocumentVersion.objects.get(pk=scope.version).revision == 1
        assert not AIArtifact.objects.filter(invocation=invocation).exists()


def test_unavailable_redis_defers_asr_without_admitting_an_invocation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    permit_asr(graph, scope, monkeypatch)
    operation = accepted(graph, scope)
    emissions: list[tuple[str, str, str]] = []
    retries: list[tuple[dict[str, str], str, int]] = []

    def metric(name: str, *, queue: str, reason: str) -> None:
        emissions.append((name, queue, reason))

    def resend(*, kwargs: dict[str, str], queue: str, countdown: int = 0) -> None:
        retries.append((kwargs, queue, countdown))

    # This is the actual fairness implementation's unavailable-backend branch,
    # not the admission seam used by the happy-path source setup above.
    monkeypatch.setattr("apps.core.fairness._broker_url", lambda: "memory://")
    monkeypatch.setattr("apps.core.fairness._emit_metric", metric)
    monkeypatch.setattr("apps.scribe.tasks.transcribe.apply_async", resend)
    with runtime_role():
        assert run_transcription(operation_id=str(operation)) == "transcription_pending"
    assert emissions == [
        ("clinic_fairness.deferred", "ai-interactive", "redis_unavailable")
    ]
    assert retries == [
        (
            {"operation_id": str(operation)},
            "ai-interactive",
            DEFER_COUNTDOWN_SECONDS,
        )
    ]
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not ChunkTranscription.objects.filter(session_id=scope.session).exists()
        assert not AIInvocation.objects.filter(purpose="scribe_asr").exists()
