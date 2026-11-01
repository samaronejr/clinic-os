"""Read source material only through its current assigned-clinician boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from apps.ai.context import assemble_context
from apps.ai.contracts import Purpose
from apps.ai.policy import enabled_policy, select_configuration
from apps.consent.services import ai_disclosure_status, consent_for_future_use
from apps.ehr.models import ClinicalDocumentVersion, DraftEditState
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.scribe.authority import EncounterScope, encounter_scope
from apps.scribe.contracts import ScribeError
from apps.scribe.draft_contracts import (
    MAX_SOURCES,
    SECTIONS,
    DraftAccessDeniedError,
    DraftConflictError,
    DraftReason,
    DraftSource,
)
from apps.scribe.models import RecordingSession, TranscriptVersion
from apps.scribe.transcripts import TranscriptAccessDeniedError, read_transcript

if TYPE_CHECKING:
    from uuid import UUID

    from apps.consent.models import ConsentAcceptance
    from apps.scribe.transcript_contracts import TranscriptVersionDTO


@dataclass(frozen=True, slots=True)
class NoteFrame:
    """SOAP and section epochs came from one statement snapshot."""

    version: ClinicalDocumentVersion
    scope: EncounterScope
    epochs: dict[str, int]


def note_frame(*, clinic_id: UUID, version_id: UUID) -> NoteFrame:
    """Never create an edit-state row, read audit or session on a refusal."""
    version = (
        ClinicalDocumentVersion.objects.select_related(
            "document__encounter", "drafteditstate"
        )
        .filter(pk=version_id, document__encounter__clinic_id=clinic_id)
        .first()
    )
    if version is None:
        raise DraftAccessDeniedError
    try:
        scope = encounter_scope(
            clinic_id=clinic_id, encounter_id=version.document.encounter_id
        )
    except ScribeError as error:
        raise DraftAccessDeniedError from error
    if (
        version.author_id != scope.physician_id
        or scope.encounter.state != "open"
        or version.state != "draft"
    ):
        raise DraftAccessDeniedError
    try:
        epochs = version.drafteditstate.section_edit_epochs
    except DraftEditState.DoesNotExist:
        epochs = {}
    return NoteFrame(version, scope, {key: int(epochs.get(key, 0)) for key in SECTIONS})


def drafting_consent(frame: NoteFrame) -> ConsentAcceptance:
    """Require AI consent and disclosure separately from recording and opt-in."""
    clinic = frame.scope.encounter.clinic_id
    enabled_policy(Purpose.SCRIBE_DRAFT)
    select_configuration(clinic_id=clinic, purpose=Purpose.SCRIBE_DRAFT)
    consent = consent_for_future_use(
        clinic_id=clinic,
        enrollment_id=frame.scope.enrollment.pk,
        purpose="ai_assistance",
    )
    disclosure = ai_disclosure_status(
        clinic_id=clinic, encounter_id=frame.scope.encounter.pk
    )
    if (
        consent is None
        or disclosure is None
        or not disclosure.informed
        or disclosure.refused
    ):
        raise DraftAccessDeniedError
    return consent


def resolve_sources(
    frame: NoteFrame, *, transcript_id: UUID | None = None
) -> tuple[TranscriptVersionDTO, tuple[DraftSource, ...]]:
    """Preserve acknowledged gaps without reconstructing any missing source."""
    clinic = frame.scope.encounter.clinic_id
    frame = note_frame(clinic_id=clinic, version_id=frame.version.pk)
    recording = (
        RecordingSession.objects.filter(
            encounter_id=frame.scope.encounter.pk, discarded=False
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    if recording is None:
        raise DraftConflictError(DraftReason.PENDING)
    try:
        transcript = read_transcript(clinic_id=clinic, session_id=recording.pk)
        if transcript is None or transcript.state != "final":
            raise DraftConflictError(DraftReason.PENDING)
        if transcript_id is not None and transcript.version_id != transcript_id:
            raise DraftConflictError(DraftReason.SOURCE)
        chart = assemble_context(
            clinic_id=clinic, encounter_id=frame.scope.encounter.pk
        )
    except (
        CurrentActorError,
        ClinicalAccessDeniedError,
        TranscriptAccessDeniedError,
    ) as error:
        raise DraftAccessDeniedError from error
    sources = [
        DraftSource(
            {
                "kind": "transcript",
                "id": str(segment.segment_id),
                "version": str(transcript.version_id),
                "revision": transcript.revision,
                "digest": sha256(segment.text.encode("utf-8")).hexdigest(),
            },
            segment.text,
            "reported"
            if segment.speaker_reviewed and segment.speaker in {"patient", "caregiver"}
            else "hypothesis",
            bool(segment.uncertainty),
        )
        for segment in transcript.segments
    ]
    for source in chart.sources:
        # A recorded problem or clinician utterance is not a newly observed exam.
        observed = (
            source.kind == "observation"
            and json.loads(source.text).get("provenance") == "observed"
        )
        sources.append(
            DraftSource(
                source.reference(), source.text, "observed" if observed else "reported"
            )
        )
    if len(sources) > MAX_SOURCES:
        raise DraftConflictError(DraftReason.CAPACITY)
    return transcript, tuple(sources)


def current_transcript_id(frame: NoteFrame) -> UUID | None:
    """Find the latest final without reading clinical text or writing an audit."""
    return (
        TranscriptVersion.objects.filter(
            session__encounter_id=frame.scope.encounter.pk,
            session__discarded=False,
        )
        .order_by("-session__created_at", "-revision")
        .values_list("pk", flat=True)
        .first()
    )
