"""Governed batch admission and immutable, generation-fenced draft projection."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.ai.artifacts import create_input
from apps.ai.contracts import Budget, GatewayRefusedError, Purpose, Usage
from apps.ai.models import AIArtifact, AIInvocation
from apps.ai.services import invoke
from apps.audit.services import record_phase1_event
from apps.scribe.draft_contracts import (
    SECTIONS,
    DraftAccessDeniedError,
    DraftConflictError,
    DraftInputError,
    DraftReason,
    DraftRequest,
    document,
    parse_suggestions,
)
from apps.scribe.draft_sources import drafting_consent, note_frame, resolve_sources
from apps.scribe.models import AIDraft, DraftActionProposal, DraftSectionSuggestion

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scribe.draft_contracts import DraftSource


def generation_lock(version_id: UUID) -> None:
    """All scribe writers take this before their first audit; no note-row lock."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"scribe-draft:{version_id}"],
        )


def current_generation(draft: AIDraft, section: str) -> bool:
    """Section regeneration invalidates that section, not unrelated suggestions."""
    return not AIDraft.objects.filter(
        version_id=draft.version_id,
        generation__gt=draft.generation,
        sections__contains=[section],
    ).exists()


@transaction.atomic
def request_draft(
    *,
    clinic_id: UUID,
    version_id: UUID,
    transcript_id: UUID,
    request_key: UUID,
    request: DraftRequest,
) -> AIDraft:
    """Nothing survives a refusal, including an input artifact or reservation."""
    expected_revision, sections = request.expected_revision, request.sections
    frame = note_frame(clinic_id=clinic_id, version_id=version_id)
    if (
        not sections
        or len(set(sections)) != len(sections)
        or not set(sections) <= set(SECTIONS)
        or type(expected_revision) is not int
        or expected_revision < 1
    ):
        raise DraftInputError
    generation_lock(version_id)
    frame = note_frame(clinic_id=clinic_id, version_id=version_id)
    previous = AIDraft.objects.filter(
        version_id=version_id, request_key=request_key
    ).first()
    if previous is not None:
        if (
            previous.base_revision != expected_revision
            or previous.transcript_id != transcript_id
            or previous.sections != list(sections)
        ):
            raise DraftConflictError(DraftReason.REPLAY)
        return previous
    if frame.version.revision != expected_revision:
        raise DraftConflictError
    consent = drafting_consent(frame)
    transcript, sources = resolve_sources(frame, transcript_id=transcript_id)
    if not sources or transcript.version_id is None:
        raise DraftConflictError(DraftReason.PENDING)
    last = AIDraft.objects.filter(version_id=version_id).order_by("-generation").first()
    generation = last.generation + 1 if last is not None else 1
    artifact = create_input(
        clinic_id=clinic_id,
        purpose=Purpose.SCRIBE_DRAFT,
        patient_enrollment_id=frame.scope.enrollment.pk,
        content=json.dumps(
            {
                "schema": "scribe-draft-reference-v1",
                "version": str(version_id),
                "generation": generation,
            },
            sort_keys=True,
        ),
    )
    invocation = invoke(
        clinic_id=clinic_id,
        purpose=Purpose.SCRIBE_DRAFT,
        principal=frame.scope.physician_id,
        inputs_ref=artifact.pk,
        budget=Budget(Usage(tokens_in=16_000, tokens_out=8_000), 1_000_000),
    )
    if invocation.reason is not None or invocation.invocation_id is None:
        raise GatewayRefusedError(invocation.reason or "configuration_unavailable")
    draft = AIDraft.objects.create(
        organization_id=frame.version.organization_id,
        clinic_id=clinic_id,
        encounter_id=frame.scope.encounter.pk,
        version=frame.version,
        transcript_id=transcript.version_id,
        consent=consent,
        invocation_id=invocation.invocation_id,
        requested_by_id=frame.scope.physician_id,
        request_key=request_key,
        generation=generation,
        base_revision=frame.version.revision,
        base_epochs=frame.epochs,
        base_content=frame.version.soap,
        sections=list(sections),
        source_refs=[source.reference for source in sources],
    )
    record_phase1_event(
        "scribe.draft.requested", clinic_id=clinic_id, affected_record_id=draft.pk
    )
    return draft


def draft_for_input(artifact: AIArtifact) -> AIDraft | None:
    """Only a committed domain-owned request can turn a gateway input into text."""
    if artifact.purpose != Purpose.SCRIBE_DRAFT:
        return None
    if '"scribe-draft-reference-v1"' not in artifact.content:
        return None
    raw = document(artifact.content)
    if raw.get("schema") != "scribe-draft-reference-v1":
        return None
    draft = AIDraft.objects.filter(invocation__inputs_ref=artifact).first()
    if draft is None or raw != {
        "schema": "scribe-draft-reference-v1",
        "version": str(draft.version_id),
        "generation": draft.generation,
    }:
        raise DraftAccessDeniedError
    return draft


def sources_for_draft(draft: AIDraft) -> tuple[DraftSource, ...]:
    """Recheck live authority, exact consent and every dependency before use."""
    frame = note_frame(clinic_id=draft.clinic_id, version_id=draft.version_id)
    consent = drafting_consent(frame)
    if consent.pk != draft.consent_id:
        raise DraftAccessDeniedError
    _transcript, sources = resolve_sources(frame, transcript_id=draft.transcript_id)
    if sorted(
        json.dumps(source.reference, sort_keys=True) for source in sources
    ) != sorted(
        json.dumps(reference, sort_keys=True) for reference in draft.source_refs
    ):
        raise DraftConflictError(DraftReason.SOURCE)
    return sources


def eligible_draft(artifact: AIArtifact) -> bool:
    """No old generation gains authority from an opaque input reference."""
    draft = draft_for_input(artifact)
    if draft is None:
        return True
    sources_for_draft(draft)
    return any(current_generation(draft, section) for section in draft.sections)


def materialize_draft(artifact: AIArtifact) -> str | None:
    """Plaintext is ephemeral and repr-hidden in the shared provider request."""
    draft = draft_for_input(artifact)
    if draft is None:
        return None
    sources = sources_for_draft(draft)
    return json.dumps(
        {
            "schema": "scribe-material-v1",
            "sections": draft.sections,
            "sources": [asdict(source) for source in sources],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def complete_draft(*, invocation_id: UUID, content: str) -> None:
    """Settle metering before projection, and project before the first AI audit."""
    invocation = AIInvocation.objects.get(pk=invocation_id)
    if invocation.purpose != Purpose.SCRIBE_DRAFT:
        return
    draft = AIDraft.objects.filter(invocation=invocation).first()
    if draft is None:
        return
    generation_lock(draft.version_id)
    active = tuple(
        section for section in draft.sections if current_generation(draft, section)
    )
    if not active:
        return
    try:
        sources = sources_for_draft(draft)
    except (DraftAccessDeniedError, DraftConflictError, GatewayRefusedError):
        # The provider charge remains settled, but a late source change earns
        # no suggestion and cannot authorize another provider attempt.
        return
    parsed = parse_suggestions(
        content,
        sources=sources,
        sections=tuple(draft.sections),
        base_revision=draft.base_revision,
        generation=draft.generation,
    )
    for item in parsed.sections:
        if item.section in active:
            DraftSectionSuggestion.objects.create(
                organization_id=draft.organization_id,
                version_id=draft.version_id,
                draft=draft,
                section=item.section,
                text=item.text,
                citations=list(item.citations),
                assertion_class=item.assertion_class,
            )
    # Proposals stay A0, separate from record_clinical_note and the inherited
    # agent approval/execution path. No new clinical tool is implicitly granted.
    if len(active) == len(draft.sections):
        for index, proposal in enumerate(parsed.proposals):
            DraftActionProposal.objects.create(
                organization_id=draft.organization_id,
                version_id=draft.version_id,
                draft=draft,
                ordinal=index,
                kind=proposal.kind,
                text=proposal.text,
                citations=list(proposal.citations),
            )
    record_phase1_event(
        "scribe.draft.generated", clinic_id=draft.clinic_id, affected_record_id=draft.pk
    )
