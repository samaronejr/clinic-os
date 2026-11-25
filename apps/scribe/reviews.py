"""Clinician-owned CAS application and durable encounter AI-use provenance."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

import rfc8785
from django.db import transaction

from apps.ai.contracts import GatewayRefusedError
from apps.ai.models import AIInvocation
from apps.ai.pending_outputs import (
    PendingKey,
    ai_pending_visible,
    decisions_visible,
    fenced_keys,
)
from apps.audit.services import record_phase1_event
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    record_clinical_note,
)
from apps.identity.current_context import CurrentActorError
from apps.scribe.draft_contracts import (
    MAX_TEXT,
    SECTIONS,
    DraftAccessDeniedError,
    DraftConflictError,
    DraftInputError,
    DraftReason,
    ReviewInput,
)
from apps.scribe.draft_sources import (
    compose_sources,
    drafting_consent,
    note_frame,
    source_recording,
)
from apps.scribe.drafting import (
    current_generation,
    generation_lock,
    sources_for_draft,
)
from apps.scribe.merge import SectionMerge, merge_section
from apps.scribe.models import (
    AIDraft,
    DraftActionProposal,
    DraftSectionSuggestion,
    ReviewDecision,
)
from apps.scribe.transcripts import (
    SnapshotRead,
    TranscriptAccessDeniedError,
    authorized_recording,
    project_transcript,
    select_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.ai.pending_outputs import RenderSelection, VisiblePending
    from apps.ehr.models import ClinicalDocumentVersion
    from apps.scribe.draft_contracts import DraftSource
    from apps.scribe.draft_sources import NoteFrame
    from apps.scribe.transcript_contracts import TranscriptVersionDTO


def note_content(content: dict[str, str]) -> None:
    """Bound the posted manual note before any domain command writes."""
    if set(content) != set(SECTIONS) or any(
        type(value) is not str or len(value) > MAX_TEXT for value in content.values()
    ):
        raise DraftInputError


@transaction.atomic
def review_suggestion(
    *,
    clinic_id: UUID,
    version_id: UUID,
    review: ReviewInput,
) -> ReviewDecision:
    """Decide every refusal before writing, and roll back a racing CAS refusal."""
    suggestion_id, expected_revision, command_id = (
        review.suggestion_id,
        review.expected_revision,
        review.command_id,
    )
    action, content, replacement = review.action, review.content, review.replacement
    note_content(content)
    frame = note_frame(clinic_id=clinic_id, version_id=version_id)
    generation_lock(version_id)
    frame = note_frame(clinic_id=clinic_id, version_id=version_id)
    digest = sha256(
        rfc8785.dumps(
            {
                "suggestion": str(suggestion_id),
                "revision": expected_revision,
                "action": action,
                "content": content,
                "replacement": replacement,
            }
        )
    ).hexdigest()
    previous = ReviewDecision.objects.filter(
        version_id=version_id, command_id=command_id
    ).first()
    if previous is not None:
        if previous.request_digest != digest:
            raise DraftConflictError(DraftReason.REPLAY)
        # A recorded human decision is not a new decision on pending output.
        return previous
    selector = PendingKey(DraftSectionSuggestion, suggestion_id)
    if not decisions_visible((selector,), clinic_id=clinic_id):
        raise DraftAccessDeniedError
    parents = fenced_keys(
        AIDraft,
        clinic=clinic_id,
        version_id=version_id,
        draftsectionsuggestion__pk=suggestion_id,
        limit=1,
    )
    if not parents:
        raise DraftAccessDeniedError

    def apply(batch: RenderSelection) -> ReviewDecision:
        suggestion, draft = batch.get(selector), batch.get(parents[0])
        if (
            suggestion is None
            or draft is None
            or suggestion.record.version_id != version_id
            or suggestion.record.draft_id != draft.record.pk
        ):
            raise DraftAccessDeniedError
        return _apply_checked(frame, suggestion, draft, review, digest)

    return ai_pending_visible((selector, parents[0]), apply, clinic_id=clinic_id)


def _apply_checked(
    frame: NoteFrame,
    selected: VisiblePending[DraftSectionSuggestion],
    parent: VisiblePending[AIDraft],
    review: ReviewInput,
    digest: str,
) -> ReviewDecision:
    suggestion, draft = selected.record, parent.record
    clinic_id, version_id = frame.scope.encounter.clinic_id, frame.version.pk
    expected_revision, command_id = review.expected_revision, review.command_id
    action, content, replacement = review.action, review.content, review.replacement
    if (
        ReviewDecision.objects.filter(suggestion_id=suggestion.pk).exists()
        or frame.version.revision != expected_revision
    ):
        raise DraftConflictError
    result = frame.version
    if action != "rejected":
        if not current_generation(draft, suggestion.section):
            raise DraftConflictError(DraftReason.GENERATION)
        sources_for_draft(parent)
        current = content[suggestion.section]
        comparison = merge_section(
            base=draft.base_content[suggestion.section],
            model=suggestion.text,
            current=current,
            edit_epochs=(
                draft.base_epochs[suggestion.section],
                frame.epochs[suggestion.section]
                + int(current != frame.version.soap[suggestion.section]),
            ),
        )
        if action == "accepted" and comparison.state == "conflict":
            raise DraftConflictError(DraftReason.SECTION)
        merged = dict(content)
        if action == "modified":
            merged[suggestion.section] = replacement or ""
        else:
            merged[suggestion.section] = comparison.text
        try:
            result = record_clinical_note(
                clinic_id=clinic_id,
                version_id=version_id,
                expected_revision=expected_revision,
                content=merged,
            )
        except ClinicalConflictError as error:
            raise DraftConflictError from error
        # Revalidate dependencies after the EHR CAS as well. A source changed
        # during the save rolls back that save, all read audits and this command.
        sources_for_draft(parent)
    invocation = AIInvocation.objects.select_related("configuration").get(
        pk=draft.invocation_id, clinic_id=clinic_id
    )
    decision = ReviewDecision.objects.create(
        organization_id=frame.version.organization_id,
        version_id=version_id,
        suggestion_id=suggestion.pk,
        reviewed_by_id=frame.scope.physician_id,
        command_id=command_id,
        request_digest=digest,
        action=action,
        section=suggestion.section,
        expected_revision=expected_revision,
        result_revision=result.revision,
        result_sha256=result.content_sha256,
        applied_text=result.soap[suggestion.section] if action != "rejected" else None,
        capability_snapshot={
            "purpose": "scribe_draft",
            "capability": invocation.configuration.capability_key,
            "configuration_id": str(invocation.configuration_id),
            "version": invocation.config_version,
        },
    )
    record_phase1_event(
        f"scribe.suggestion.{action}",
        clinic_id=clinic_id,
        affected_record_id=decision.pk,
    )
    return decision


@dataclass(frozen=True, slots=True)
class DraftRevision:
    """Immutable generation metadata needed beside a checked suggestion."""

    base_revision: int
    generation: int


@dataclass(frozen=True, slots=True)
class SectionPreview:
    """Approved-for-display fields, with no deferred model relations."""

    pk: UUID
    section: str
    text: str
    citations: tuple[dict[str, object], ...]
    assertion_class: str
    draft: DraftRevision


@dataclass(frozen=True, slots=True)
class ProposalPreview:
    """The display-only fields of a separate A0 suggestion."""

    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class ReviewSources:
    """A source binding shared by every section in this projection."""

    version_id: UUID | None
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuggestionRow:
    """Review state is distinct from the authored document state."""

    suggestion: SectionPreview
    base: str
    current: str
    state: str
    comparison: SectionMerge
    decision: ReviewDecision | None


@dataclass(frozen=True, slots=True)
class ReviewPanel:
    """One authorized native review screen, with a complete manual-note path."""

    version: ClinicalDocumentVersion
    transcript: TranscriptVersionDTO | None
    sources: tuple[DraftSource, ...]
    rows: tuple[SuggestionRow, ...]
    proposals: tuple[ProposalPreview, ...]
    available: bool
    latest: DraftRevision | None


@transaction.atomic
def read_review(*, clinic_id: UUID, version_id: UUID) -> ReviewPanel:
    """Resolve sources once; an unavailable feature never blocks manual care."""
    return render_review(clinic_id=clinic_id, version_id=version_id, emit=lambda p: p)


def render_review[R](
    *, clinic_id: UUID, version_id: UUID, emit: Callable[[ReviewPanel], R]
) -> R:
    """Render a note and its permitted AI overlay in one projection frame."""
    frame = note_frame(clinic_id=clinic_id, version_id=version_id)
    try:
        consent = drafting_consent(frame)
    except (GatewayRefusedError, DraftAccessDeniedError):
        return emit(_manual_panel(frame))
    recording = None
    snapshot = SnapshotRead(None)
    with suppress(DraftConflictError):
        try:
            recording = authorized_recording(
                clinic_id=clinic_id, session_id=source_recording(frame).pk
            )
        except TranscriptAccessDeniedError as error:
            raise DraftAccessDeniedError from error
        snapshot = select_snapshot(recording)
    drafts = fenced_keys(
        AIDraft,
        clinic=clinic_id,
        version_id=version_id,
        consent_id=consent.pk,
        ordering=("-generation",),
        limit=50,
    )
    suggestions = fenced_keys(
        DraftSectionSuggestion,
        clinic=clinic_id,
        draft_id__in=[d.pk for d in drafts],
        ordering=("section", "-draft__generation"),
        distinct_on=("section",),
    )
    proposals = (
        fenced_keys(
            DraftActionProposal,
            clinic=clinic_id,
            draft_id=drafts[0].pk,
            ordering=("ordinal",),
        )
        if drafts
        else ()
    )
    decisions = {
        decision.suggestion_id: decision
        for decision in ReviewDecision.objects.filter(
            suggestion_id__in=[suggestion.pk for suggestion in suggestions]
        )
    }

    def project(batch: RenderSelection) -> R:
        try:
            if drafting_consent(frame).pk != consent.pk:
                return emit(_manual_panel(frame))
        except (GatewayRefusedError, DraftAccessDeniedError):
            return emit(_manual_panel(frame))
        transcript = None
        sources: tuple[DraftSource, ...] = ()
        if recording is not None:
            try:
                projected = project_transcript(recording, snapshot, batch)
            except (
                CurrentActorError,
                ClinicalAccessDeniedError,
                TranscriptAccessDeniedError,
            ) as error:
                # The same denial resolve_sources gives the drafting path.
                raise DraftAccessDeniedError from error
            with suppress(DraftConflictError):
                transcript, sources = compose_sources(frame, projected)
        binding = ReviewSources(
            transcript.version_id if transcript else None,
            tuple(sorted(json.dumps(s.reference, sort_keys=True) for s in sources)),
        )
        rows = tuple(
            _suggestion_row(
                frame,
                suggestion,
                draft,
                decisions.get(suggestion.record.pk),
                binding,
            )
            for suggestion in batch.many(suggestions)
            if (draft := batch.get(PendingKey(AIDraft, suggestion.record.draft_id)))
            is not None
        )
        latest = batch.get(drafts[0]) if drafts else None
        return emit(
            ReviewPanel(
                frame.version,
                transcript,
                sources,
                rows,
                tuple(_proposal_preview(checked) for checked in batch.many(proposals))
                if latest
                else (),
                transcript is not None
                and transcript.state == "final"
                and bool(sources),
                _draft_revision(latest) if latest else None,
            )
        )

    return ai_pending_visible(
        (
            *snapshot.keys,
            *drafts,
            *suggestions,
            *proposals,
        ),
        project,
        clinic_id=clinic_id,
    )


def _manual_panel(frame: NoteFrame) -> ReviewPanel:
    return ReviewPanel(frame.version, None, (), (), (), available=False, latest=None)


def _draft_revision(checked: VisiblePending[AIDraft]) -> DraftRevision:
    return DraftRevision(checked.record.base_revision, checked.record.generation)


def _proposal_preview(checked: VisiblePending[DraftActionProposal]) -> ProposalPreview:
    return ProposalPreview(checked.record.kind, checked.record.text)


def _suggestion_row(
    frame: NoteFrame,
    checked: VisiblePending[DraftSectionSuggestion],
    parent: VisiblePending[AIDraft],
    decision: ReviewDecision | None,
    binding: ReviewSources,
) -> SuggestionRow:
    suggestion, draft = checked.record, parent.record
    section = suggestion.section
    base = draft.base_content[section]
    current = frame.version.soap[section]
    fresh = (
        binding.version_id is not None
        and draft.transcript_id == binding.version_id
        and current_generation(draft, section)
        and binding.references
        == tuple(sorted(json.dumps(r, sort_keys=True) for r in draft.source_refs))
    )
    comparison = merge_section(
        base=base,
        model=suggestion.text,
        current=current,
        edit_epochs=(draft.base_epochs[section], frame.epochs[section]),
        dependencies_current=fresh,
    )
    return SuggestionRow(
        SectionPreview(
            suggestion.pk,
            section,
            suggestion.text,
            tuple(suggestion.citations),
            suggestion.assertion_class,
            _draft_revision(parent),
        ),
        base,
        current,
        decision.action if decision else "pending" if fresh else "stale",
        comparison,
        decision,
    )
