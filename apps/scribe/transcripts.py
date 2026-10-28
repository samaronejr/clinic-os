"""Assigned-clinician reads and append-only, revision-fenced transcript review."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from django.db import IntegrityError, connection, transaction

from apps.audit.services import record_phase1_event
from apps.scribe.authority import recording_scope
from apps.scribe.contracts import ScribeError
from apps.scribe.models import RecordingSession, TranscriptSegment, TranscriptVersion
from apps.scribe.transcript_contracts import (
    MAX_COVERAGE_BYTES,
    CoverageSlot,
    Segment,
    SegmentReview,
    TranscriptError,
    TranscriptReason,
    TranscriptVersionDTO,
    Uncertainty,
    finalize,
    revise,
)

if TYPE_CHECKING:
    from apps.scribe.models import ChunkTranscription

type Mutation = Literal["recognized", "finalized", "speaker_relabeled", "corrected"]


class TranscriptAccessDeniedError(PermissionError):
    """Unknown, foreign, reassigned and discarded recordings reveal nothing."""

    def __init__(self) -> None:
        """Keep even an exception's repr free of record selectors."""
        message = "transcript access denied"
        super().__init__(message)


def authorized_recording(*, clinic_id: UUID, session_id: UUID) -> RecordingSession:
    """Recheck real assignment and patient-scoped permission for every read."""
    try:
        recording, _scope = recording_scope(
            clinic_id=clinic_id,
            session_id=session_id,
            consent_required=False,
        )
    except ScribeError as error:
        raise TranscriptAccessDeniedError from error
    if recording.discarded:
        raise TranscriptAccessDeniedError
    return recording


def coverage_for(recording: RecordingSession) -> tuple[CoverageSlot, ...]:
    """Use the same DB-generated expected slots enforced by the insert guard."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.scribe_transcript_coverage(%s)",
            [recording.pk],
        )
        row = cursor.fetchone()
    if row is None:
        raise TranscriptAccessDeniedError
    raw = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return tuple(
        CoverageSlot(
            UUID(slot["epoch_id"]),
            slot["track"],
            slot["sequence"],
            UUID(slot["chunk_id"]) if slot["chunk_id"] is not None else None,
        )
        for slot in raw
    )


def current_snapshot(recording: RecordingSession) -> TranscriptVersionDTO:
    """Read one immutable snapshot; the caller owns the surrounding tenant txn."""
    current = (
        TranscriptVersion.objects.filter(session=recording)
        .order_by("-revision")
        .first()
    )
    if current is None:
        return TranscriptVersionDTO(
            0,
            "partial",
            (),
            coverage_for(recording),
            frozenset(),
            recording.state in {"stopped", "stopped_revoked"},
        )
    source_rows = (
        TranscriptSegment.objects.filter(
            session=recording,
            source_id__in=current.coverage["sources"],
            version__revision__lte=current.revision,
        )
        .order_by("source_id", "-version__revision")
        .distinct("source_id")
    )
    values = tuple(
        sorted(
            (
                Segment(
                    row.source_id,
                    row.chunk_id,
                    row.start_ms,
                    row.end_ms,
                    row.text,
                    row.speaker,
                    row.confidence_milli,
                    tuple(Uncertainty(**span) for span in row.uncertainty),
                    row.language,
                    row.speaker_reviewed,
                    row.text_reviewed,
                )
                for row in source_rows
            ),
            key=lambda value: (
                value.start_ms,
                value.end_ms,
                value.chunk_id.int,
                value.segment_id.int,
            ),
        )
    )
    return TranscriptVersionDTO(
        current.revision,
        "final" if current.state == "final" else "partial",
        values,
        tuple(
            CoverageSlot(
                UUID(slot["epoch_id"]),
                slot["track"],
                slot["sequence"],
                UUID(slot["chunk_id"]) if slot["chunk_id"] is not None else None,
            )
            for slot in current.coverage["slots"]
        ),
        frozenset(UUID(value) for value in current.coverage["completed"]),
        current.coverage["recording_closed"],
        current.incomplete_acknowledged,
        current.pk,
    )


def refresh_coverage(
    value: TranscriptVersionDTO,
    recording: RecordingSession,
) -> TranscriptVersionDTO:
    """Refresh only an unfinished snapshot; never rewrite a finalized record."""
    if value.state == "final":
        return value
    coverage = coverage_for(recording)
    accepted = {slot.chunk_id for slot in coverage if slot.chunk_id is not None}
    return replace(
        value,
        coverage=coverage,
        completed_chunks=value.completed_chunks.intersection(accepted),
        segments=tuple(item for item in value.segments if item.chunk_id in accepted),
        recording_closed=recording.state in {"stopped", "stopped_revoked"},
    )


@dataclass(frozen=True, slots=True)
class RecognitionReceipt:
    """Only ASR successors carry their immutable request and event provenance."""

    request: ChunkTranscription
    index: int
    latency_ms: int


def save_snapshot(
    recording: RecordingSession,
    value: TranscriptVersionDTO,
    *,
    mutation: Mutation,
    recognition: RecognitionReceipt | None = None,
    changed_source: UUID | None = None,
) -> TranscriptVersionDTO:
    """Write a complete snapshot and encrypted segments in the same transaction."""
    previous = (
        TranscriptVersion.objects.filter(session=recording)
        .order_by("-revision")
        .first()
    )
    if value.revision != (previous.revision + 1 if previous is not None else 1):
        raise TranscriptError(TranscriptReason.STALE)
    coverage = {
        "slots": [
            {
                "epoch_id": str(slot.epoch_id),
                "track": slot.track,
                "sequence": slot.sequence,
                "chunk_id": str(slot.chunk_id) if slot.chunk_id is not None else None,
            }
            for slot in value.coverage
        ],
        "completed": sorted(str(chunk) for chunk in value.completed_chunks),
        "sources": sorted(str(item.segment_id) for item in value.segments),
        "recording_closed": value.recording_closed,
    }
    if len(json.dumps(coverage).encode("utf-8")) > MAX_COVERAGE_BYTES:
        raise TranscriptError(TranscriptReason.LIMIT)
    try:
        with transaction.atomic():
            stored = TranscriptVersion.objects.create(
                organization_id=recording.organization_id,
                session=recording,
                predecessor=previous,
                revision=value.revision,
                state=value.state,
                mutation=mutation,
                incomplete_acknowledged=value.incomplete_acknowledged,
                coverage=coverage,
                request=recognition.request if recognition is not None else None,
                recognition_index=recognition.index
                if recognition is not None
                else None,
                latency_ms=recognition.latency_ms if recognition is not None else 0,
                created_by_id=recording.physician_id,
            )
    except IntegrityError as error:
        constraint = getattr(
            getattr(error.__cause__, "diag", None), "constraint_name", None
        )
        if constraint in {
            "scribe_transcript_coverage_changed",
            "scribe_transcript_capacity",
        }:
            raise TranscriptError(
                TranscriptReason.STALE
                if constraint == "scribe_transcript_coverage_changed"
                else TranscriptReason.LIMIT
            ) from error
        raise
    TranscriptSegment.objects.bulk_create(
        [
            TranscriptSegment(
                organization_id=recording.organization_id,
                session=recording,
                version=stored,
                source_id=item.segment_id,
                chunk_id=item.chunk_id,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                text=item.text,
                speaker=item.speaker,
                confidence_milli=item.confidence_milli,
                uncertainty=[asdict(span) for span in item.uncertainty],
                language=item.language,
                speaker_reviewed=item.speaker_reviewed,
                text_reviewed=item.text_reviewed,
            )
            for item in value.segments
            if (
                recognition is not None
                and item.chunk_id == recognition.request.chunk_id
            )
            or item.segment_id == changed_source
        ]
    )
    record_phase1_event(
        f"scribe.transcript.{mutation}",
        clinic_id=recording.clinic_id,
        affected_record_id=stored.pk,
    )
    return replace(value, version_id=stored.pk)


def read_transcript(
    *,
    clinic_id: UUID,
    session_id: UUID,
) -> TranscriptVersionDTO | None:
    """Return no source until the caller's own assigned encounter is resolved."""
    recording = authorized_recording(clinic_id=clinic_id, session_id=session_id)
    value = current_snapshot(recording)
    return refresh_coverage(value, recording) if value.revision else None


@transaction.atomic
def finalize_transcript(
    *,
    clinic_id: UUID,
    session_id: UUID,
    expected_revision: int,
    acknowledge_incomplete: bool,
) -> TranscriptVersionDTO:
    """Serialize against completion/review and verify the current coverage."""
    authorized_recording(clinic_id=clinic_id, session_id=session_id)
    recording = RecordingSession.objects.select_for_update().get(pk=session_id)
    value = refresh_coverage(current_snapshot(recording), recording)
    if value.revision != expected_revision:
        raise TranscriptError(TranscriptReason.STALE)
    return save_snapshot(
        recording,
        finalize(value, acknowledge_incomplete=acknowledge_incomplete),
        mutation="finalized",
    )


@transaction.atomic
def review_segment(
    *,
    clinic_id: UUID,
    session_id: UUID,
    expected_revision: int,
    review: SegmentReview,
) -> TranscriptVersionDTO:
    """Human corrections are new versions, never an update to a prior final."""
    authorized_recording(clinic_id=clinic_id, session_id=session_id)
    recording = RecordingSession.objects.select_for_update().get(pk=session_id)
    value = refresh_coverage(current_snapshot(recording), recording)
    if value.revision != expected_revision:
        raise TranscriptError(TranscriptReason.STALE)
    return save_snapshot(
        recording,
        revise(
            value,
            segment_id=review.segment_id,
            speaker=review.speaker,
            text=review.text,
            uncertainty=review.uncertainty,
        ),
        mutation="corrected" if review.text is not None else "speaker_relabeled",
        changed_source=review.segment_id,
    )
