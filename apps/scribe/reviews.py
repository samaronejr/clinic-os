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
from apps.audit.services import record_phase1_event
from apps.ehr.services import ClinicalConflictError, record_clinical_note
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
    drafting_consent,
    note_frame,
    resolve_sources,
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

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ehr.models import ClinicalDocumentVersion
    from apps.scribe.draft_contracts import DraftSource
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
        return previous
    suggestion = (
        DraftSectionSuggestion.objects.select_related(
            "draft__invocation__configuration"
        )
        .filter(pk=suggestion_id, version_id=version_id)
        .first()
    )
    if suggestion is None:
        raise DraftAccessDeniedError
    if (
        ReviewDecision.objects.filter(suggestion=suggestion).exists()
        or frame.version.revision != expected_revision
    ):
        raise DraftConflictError
    draft = suggestion.draft
    result = frame.version
    if action != "rejected":
        if not current_generation(draft, suggestion.section):
            raise DraftConflictError(DraftReason.GENERATION)
        sources_for_draft(draft)
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
        sources_for_draft(draft)
    invocation = draft.invocation
    decision = ReviewDecision.objects.create(
        organization_id=frame.version.organization_id,
        version_id=version_id,
        suggestion=suggestion,
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
class SuggestionRow:
    """Review state is distinct from the authored document state."""

    suggestion: DraftSectionSuggestion
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
    proposals: tuple[DraftActionProposal, ...]
    available: bool
    latest: AIDraft | None


@transaction.atomic
def read_review(*, clinic_id: UUID, version_id: UUID) -> ReviewPanel:
    """Resolve sources once; an unavailable feature never blocks manual care."""
    frame = note_frame(clinic_id=clinic_id, version_id=version_id)
    transcript = None
    sources: tuple[DraftSource, ...] = ()
    with suppress(DraftConflictError):
        transcript, sources = resolve_sources(frame)
    available = False
    try:
        drafting_consent(frame)
        available = (
            transcript is not None and transcript.state == "final" and bool(sources)
        )
    except (GatewayRefusedError, DraftAccessDeniedError):
        pass
    drafts = tuple(
        AIDraft.objects.filter(version_id=version_id).order_by("-generation")[:50]
    )
    rows: list[SuggestionRow] = []
    references = sorted(
        json.dumps(source.reference, sort_keys=True) for source in sources
    )
    for suggestion in (
        DraftSectionSuggestion.objects.filter(draft__in=drafts)
        .select_related("draft", "reviewdecision")
        .order_by("section", "-draft__generation")
        .distinct("section")
    ):
        draft = suggestion.draft
        try:
            decision = suggestion.reviewdecision
        except ReviewDecision.DoesNotExist:
            decision = None
        current = frame.version.soap[suggestion.section]
        fresh = (
            transcript is not None
            and draft.transcript_id == transcript.version_id
            and current_generation(draft, suggestion.section)
            and references
            == sorted(
                json.dumps(reference, sort_keys=True) for reference in draft.source_refs
            )
        )
        comparison = merge_section(
            base=draft.base_content[suggestion.section],
            model=suggestion.text,
            current=current,
            edit_epochs=(
                draft.base_epochs[suggestion.section],
                frame.epochs[suggestion.section],
            ),
            dependencies_current=fresh,
        )
        rows.append(
            SuggestionRow(
                suggestion,
                draft.base_content[suggestion.section],
                current,
                decision.action if decision else "pending" if fresh else "stale",
                comparison,
                decision,
            )
        )
    latest = drafts[0] if drafts else None
    proposals = (
        tuple(DraftActionProposal.objects.filter(draft=latest).order_by("ordinal"))
        if latest is not None
        else ()
    )
    return ReviewPanel(
        frame.version, transcript, sources, tuple(rows), proposals, available, latest
    )
