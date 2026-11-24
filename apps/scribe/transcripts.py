"""Assigned-clinician reads and append-only, revision-fenced transcript review."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from django.db import IntegrityError, connection, transaction

from apps.ai.pending_outputs import PendingKey, ai_pending_visible
from apps.ai.pending_outputs import rows as pending_rows
from apps.audit.services import record_phase1_event
from apps.scribe.authority import recording_scope, transcription_consent
from apps.scribe.contracts import ScribeError
from apps.scribe.models import (
    ChunkTranscription,
    RecordingSession,
    TranscriptSegment,
    TranscriptVersion,
)
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
    from apps.ai.pending_outputs import RenderSelection, VisiblePending

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


@dataclass(frozen=True, slots=True)
class SnapshotRead:
    """Selectors for one snapshot; nothing here authorizes a text projection."""

    version: PendingKey[TranscriptVersion] | None
    segments: tuple[PendingKey[TranscriptSegment], ...] = ()

    @property
    def keys(self) -> tuple[PendingKey[TranscriptVersion | TranscriptSegment], ...]:
        """Provide all snapshot dependencies to the response batch."""
        return () if self.version is None else (self.version, *self.segments)


def select_snapshot(recording: RecordingSession) -> SnapshotRead:
    """Select the current revision and its exact immutable segment revisions."""
    current = (
        pending_rows(TranscriptVersion)
        .filter(session=recording)
        .order_by("-revision")
        .values_list("pk", "revision", "coverage")
        .first()
    )
    if current is None:
        return SnapshotRead(None)
    version_id, revision, coverage = current
    segment_ids = (
        pending_rows(TranscriptSegment)
        .filter(
            session=recording,
            source_id__in=coverage["sources"],
            version__revision__lte=revision,
        )
        .order_by("source_id", "-version__revision")
        .distinct("source_id")
        .values_list("pk", flat=True)
    )
    return SnapshotRead(
        PendingKey(TranscriptVersion, version_id),
        tuple(PendingKey(TranscriptSegment, pk) for pk in segment_ids),
    )


def _snapshot(
    version: VisiblePending[TranscriptVersion],
    segments: tuple[VisiblePending[TranscriptSegment], ...],
) -> TranscriptVersionDTO:
    current = version.record
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
                for checked in segments
                for row in (checked.record,)
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


def current_snapshot(recording: RecordingSession) -> TranscriptVersionDTO:
    """Project the current revision for the revision-fenced write workflows."""
    selection = select_snapshot(recording)

    def project(batch: RenderSelection) -> TranscriptVersionDTO:
        checked = batch.get(selection.version) if selection.version else None
        if checked is not None:
            return _snapshot(checked, batch.many(selection.segments))
        return TranscriptVersionDTO(
            0,
            "partial",
            (),
            coverage_for(recording),
            frozenset(),
            recording.state in {"stopped", "stopped_revoked"},
        )

    return ai_pending_visible(selection.keys, project)


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
        pending_rows(TranscriptVersion)
        .filter(session=recording)
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
            stored = pending_rows(TranscriptVersion).create(
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
    pending_rows(TranscriptSegment).bulk_create(
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
    snapshot = select_snapshot(recording)
    return ai_pending_visible(
        snapshot.keys, lambda batch: project_transcript(recording, snapshot, batch)
    )


def project_transcript(
    recording: RecordingSession, snapshot: SnapshotRead, batch: RenderSelection
) -> TranscriptVersionDTO | None:
    """Build transcript text only from the issuing visibility frame."""
    checked = batch.get(snapshot.version) if snapshot.version else None
    if checked is None:
        return None
    value = _snapshot(checked, batch.many(snapshot.segments))
    return _display_snapshot(refresh_coverage(value, recording), recording)


def _display_snapshot(
    value: TranscriptVersionDTO, recording: RecordingSession
) -> TranscriptVersionDTO:
    """Never return an unreviewed hypothesis from an earlier AI acceptance."""
    if value.state == "final" or not value.segments:
        return value
    permitted = _current_chunk_ids(recording)
    return replace(
        value,
        segments=tuple(
            item
            for item in value.segments
            if item.text_reviewed or item.chunk_id in permitted
        ),
    )


def _current_chunk_ids(recording: RecordingSession) -> set[UUID]:
    consent = transcription_consent(recording)
    if consent is None:
        return set()
    return set(
        ChunkTranscription.objects.filter(
            session=recording, consent_id=consent.pk
        ).values_list("chunk_id", flat=True)
    )


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
    if len(_display_snapshot(value, recording).segments) != len(value.segments):
        raise TranscriptAccessDeniedError
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
    visible = _display_snapshot(value, recording)
    if not any(item.segment_id == review.segment_id for item in visible.segments):
        raise TranscriptAccessDeniedError
    stored = save_snapshot(
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
    return _display_snapshot(stored, recording)
