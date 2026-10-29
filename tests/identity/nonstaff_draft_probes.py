"""Real draft authority adapters, seeded once on the census clinical subjects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.ai.models import AIArtifact
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_ai_disclosure,
    record_consent,
)
from apps.core.integration import execute_operation
from apps.ehr.finalization import amend_document
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.intake.patient_access import patient_session_context
from apps.scribe.contracts import ScribeError
from apps.scribe.draft_contracts import (
    DraftAccessDeniedError,
    DraftRequest,
    ReviewInput,
)
from apps.scribe.draft_sources import (
    NoteFrame,
    drafting_consent,
    note_frame,
    resolve_sources,
)
from apps.scribe.drafting import (
    draft_for_input,
    request_draft,
    sources_for_draft,
)
from apps.scribe.models import AIDraft, DraftSectionSuggestion, RecordingSession
from apps.scribe.outbox import register_gateway
from apps.scribe.review_views import _command, note_review
from apps.scribe.reviews import review_suggestion
from apps.scribe.transcripts import read_transcript
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.test import RequestFactory, override_settings
from django.urls import resolve, reverse

from auth.stepup_test_support import verified_request
from ehr.test_autosave import registration
from identity.nonstaff_differential import DifferentialProbe
from patient_service_support import runtime_role
from renewal.scribe_review_fixture import seed as seed_review_sources
from scribe.test_transcript_runtime import admit_interactive_work

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from identity.nonstaff_subjects import NonstaffSubjects
    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class DraftSubjects:
    graph: RbacGraph
    frame: NoteFrame
    draft: AIDraft
    artifact: AIArtifact
    suggestion: DraftSectionSuggestion
    content: dict[str, str] = field(repr=False)


def draft_subjects(
    graph: RbacGraph, *, version: UUID, transcript: UUID
) -> DraftSubjects:
    """Use one real admitted generation for every allow and deny adapter."""
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        frame = note_frame(clinic_id=graph.clinic_a, version_id=version)
        draft = request_draft(
            clinic_id=graph.clinic_a,
            version_id=version,
            transcript_id=transcript,
            request_key=uuid4(),
            request=DraftRequest(frame.version.revision),
        )
        operation = draft.invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        suggestion = DraftSectionSuggestion.objects.filter(draft=draft).first()
        assert suggestion is not None
        artifact = AIArtifact.objects.get(pk=draft.invocation.inputs_ref_id)
        return DraftSubjects(
            graph, frame, draft, artifact, suggestion, frame.version.soap
        )


def draft_calls(subject: DraftSubjects, actor: UUID) -> dict[str, Callable[[], bool]]:
    """Every call has valid records; only the bound actor changes its authority."""
    graph, frame = subject.graph, subject.frame
    clinic, version = graph.clinic_a, frame.version.pk
    authentication = verified_request(actor)
    authentication.session[f"ehr.encounter.{clinic}"] = str(frame.scope.encounter.pk)
    authentication.session[f"workspace.patient.{clinic}"] = str(
        frame.scope.enrollment.pk
    )
    authentication.session.save()
    path = reverse("scribe:review", args=(clinic,))
    request = RequestFactory().post(
        path,
        {
            "action": "open",
            "encounter_id": str(frame.scope.encounter.pk),
            "version_id": str(version),
        },
    )
    request.user = authentication.user
    request.session = authentication.session
    request.resolver_match = resolve(path)
    review = ReviewInput(
        subject.suggestion.pk,
        frame.version.revision,
        uuid4(),
        "rejected",
        subject.content,
    )
    return {
        "apps.scribe.draft_sources.drafting_consent": lambda: (
            drafting_consent(frame).pk == subject.draft.consent_id
        ),
        "apps.scribe.draft_sources.note_frame": lambda: (
            note_frame(clinic_id=clinic, version_id=version).version.pk == version
        ),
        "apps.scribe.draft_sources.resolve_sources": lambda: bool(
            resolve_sources(frame, transcript_id=subject.draft.transcript_id)[1]
        ),
        "apps.scribe.drafting.draft_for_input": lambda: (
            draft_for_input(subject.artifact) is not None
        ),
        "apps.scribe.drafting.sources_for_draft": lambda: bool(
            sources_for_draft(subject.draft)
        ),
        "apps.scribe.review_views._command": lambda: (
            _command(request, clinic).version_id == version
        ),
        "apps.scribe.review_views.note_review": lambda: (
            note_review(request, clinic).status_code == 200
        ),
        "apps.scribe.reviews.review_suggestion": lambda: (
            review_suggestion(
                clinic_id=clinic, version_id=version, review=review
            ).action
            == "rejected"
        ),
    }


def draft_probe(symbol: str, invoke: Callable[[], bool]) -> DifferentialProbe:
    """Only declared domain refusals become a negative authority decision."""

    def call() -> bool:
        try:
            return invoke()
        except (
            CurrentActorError,
            ClinicalAccessDeniedError,
            DraftAccessDeniedError,
            GatewayAccessDeniedError,
            GatewayRefusedError,
            ScribeError,
        ):
            return False

    return DifferentialProbe(symbol, call, expected=False)


def census_draft(data: NonstaffSubjects, graph: RbacGraph) -> DraftSubjects:
    """Add source artifacts to the existing census encounter, never reseed it."""
    secret_root = settings.CLINIC_SECRET_DIR
    assert secret_root is not None, "draft census requires a synthetic secret directory"
    registration(graph, graph.physician, graph.clinic_a)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="consultation_recording",
            text="SINTETICO-42: gravacao para o censo de autoridade.",
        )
    with runtime_role(), patient_session_context(data.op.patient_session):
        _text, offer = prepare_acceptance(text_id=text.pk)
        record_consent(offer=offer, purpose="consultation_recording", accepted=True)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=data.legacy.encounter,
            informed=True,
            refused=False,
        )
        # The shared operational census deliberately finalized and released the
        # original note. Open one real amendment without replacing that subject.
        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=data.legacy.version.pk,
            reason="Synthetic source-linked draft census",
        )
        assert amendment.amendment_of_id == data.legacy.version.pk
        assert amendment.document_id == data.legacy.version.document_id
    with (
        pytest.MonkeyPatch.context() as patch,
        override_settings(
            SCRIBE_SYNTHETIC_CAPTURE=True,
            SCRIBE_MEDIA_ROOT=Path(secret_root) / "scribe-census-media",
        ),
    ):
        admit_interactive_work._get_wrapped_function()(patch)
        register_gateway()
        version = seed_review_sources(
            actor=graph.physician,
            clinic=graph.clinic_a,
            organization=graph.organization_a,
            appointment=data.legacy.appointment.pk,
        )
        assert version == amendment.pk
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            recording = RecordingSession.objects.get(encounter_id=data.legacy.encounter)
            transcript = read_transcript(
                clinic_id=graph.clinic_a, session_id=recording.pk
            )
            assert transcript is not None
            assert transcript.version_id is not None
            assert transcript.state == "final"
        return draft_subjects(graph, version=version, transcript=transcript.version_id)


def draft_probes(data: NonstaffSubjects, graph: RbacGraph) -> list[DifferentialProbe]:
    """Observe all eight candidates on valid, shared records and a nonstaff actor."""
    calls = draft_calls(census_draft(data, graph), data.actor.pk)
    return [draft_probe(symbol, invoke) for symbol, invoke in sorted(calls.items())]
