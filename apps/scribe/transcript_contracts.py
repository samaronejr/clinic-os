"""Immutable transcript snapshots, explicit coverage and time-based stitching."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from apps.scribe.contracts import MAX_SEQUENCE

SPEAKERS: Final = ("clinician", "patient", "caregiver", "unknown")
MAX_SEGMENTS: Final = 100_000
MAX_TEXT: Final = 16_384
MAX_TIMELINE_MS: Final = 86_400_000
MAX_CONFIDENCE: Final = 1000
MAX_COVERAGE_BYTES: Final = 1_048_576
type TranscriptState = Literal["partial", "final"]
type UncertaintyKind = Literal["uncertain", "inaudible"]


class TranscriptReason(StrEnum):
    """Closed reasons contain no clinical data or provider exception messages."""

    INVALID = "invalid_transcript"
    INCOMPLETE = "coverage_incomplete"
    OPEN = "recording_open"
    FINAL = "transcript_final"
    STALE = "transcript_stale"
    BLOCKED = "blocked_on_eg"
    LIMIT = "transcript_capacity"


class TranscriptError(ValueError):
    """Carry only a closed, content-free reason across worker/UI boundaries."""

    def __init__(
        self,
        code: TranscriptReason = TranscriptReason.INVALID,
    ) -> None:
        """Never include source text, provider errors or record identifiers."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Uncertainty:
    """Half-open character offsets in the exact segment text, not a diagnosis."""

    start: int
    end: int
    kind: UncertaintyKind

    def __post_init__(self) -> None:
        """Validate offsets before associating them with a bounded text value."""
        if (
            type(self.start) is not int
            or type(self.end) is not int
            or not 0 <= self.start < self.end <= MAX_TEXT
            or self.kind not in {"uncertain", "inaudible"}
        ):
            raise TranscriptError


@dataclass(frozen=True, slots=True)
class Segment:
    """Clinical text never participates in repr, ordering, identity or dedup."""

    segment_id: UUID
    chunk_id: UUID
    start_ms: int
    end_ms: int
    text: str = field(repr=False)
    speaker: str = "unknown"
    confidence_milli: int | None = None
    uncertainty: tuple[Uncertainty, ...] = field(default=(), repr=False)
    language: str = "pt-BR"
    speaker_reviewed: bool = False
    text_reviewed: bool = False

    def __post_init__(self) -> None:
        """Reject unbounded timing, labels, language tags and uncertainty spans."""
        if (
            not isinstance(self.segment_id, UUID)
            or not isinstance(self.chunk_id, UUID)
            or type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms <= MAX_TIMELINE_MS
            or type(self.text) is not str
            or not 1 <= len(self.text) <= MAX_TEXT
            or self.speaker not in SPEAKERS
            or (
                self.confidence_milli is not None
                and (
                    type(self.confidence_milli) is not int
                    or not 0 <= self.confidence_milli <= MAX_CONFIDENCE
                )
            )
            or not isinstance(self.uncertainty, tuple)
            or any(
                not isinstance(span, Uncertainty) or span.end > len(self.text)
                for span in self.uncertainty
            )
            or len(self.uncertainty) > MAX_TEXT
            or not isinstance(self.language, str)
            or re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8}){0,3}", self.language)
            is None
        ):
            raise TranscriptError


@dataclass(frozen=True, slots=True)
class CoverageSlot:
    """One declared epoch/track/sequence; a missing chunk remains explicit."""

    epoch_id: UUID
    track: str
    sequence: int
    chunk_id: UUID | None

    def __post_init__(self) -> None:
        """Keep the coverage domain identical to the capture protocol."""
        if (
            not isinstance(self.epoch_id, UUID)
            or self.track not in {"microphone", "remote"}
            or type(self.sequence) is not int
            or not 0 <= self.sequence <= MAX_SEQUENCE
            or (self.chunk_id is not None and not isinstance(self.chunk_id, UUID))
        ):
            raise TranscriptError


@dataclass(frozen=True, slots=True)
class TranscriptVersionDTO:
    """A successor is a new value; even a finalized snapshot never mutates."""

    revision: int
    state: TranscriptState
    segments: tuple[Segment, ...] = field(repr=False)
    coverage: tuple[CoverageSlot, ...]
    completed_chunks: frozenset[UUID]
    recording_closed: bool
    incomplete_acknowledged: bool = False
    version_id: UUID | None = None

    @property
    def missing(self) -> tuple[CoverageSlot, ...]:
        """Include upload gaps and pending/partial/failed recognitions alike."""
        return tuple(
            slot
            for slot in self.coverage
            if slot.chunk_id is None or slot.chunk_id not in self.completed_chunks
        )


@dataclass(frozen=True, slots=True)
class ChunkUpdate:
    """One complete replacement of a chunk hypothesis, not text-level dedup."""

    chunk_id: UUID
    segments: tuple[Segment, ...] = field(repr=False)
    complete: bool


@dataclass(frozen=True, slots=True)
class SegmentReview:
    """Human changes never inherit trust from a provider's proposed label."""

    segment_id: UUID
    speaker: str | None = None
    text: str | None = field(default=None, repr=False)
    uncertainty: tuple[Uncertainty, ...] | None = field(default=None, repr=False)


def _carry_review(value: Segment, previous: Segment | None) -> Segment:
    """Keep human review only for the same immutable source time interval."""
    if previous is None or (
        previous.chunk_id,
        previous.start_ms,
        previous.end_ms,
    ) != (value.chunk_id, value.start_ms, value.end_ms):
        return value
    return replace(
        value,
        speaker=previous.speaker if previous.speaker_reviewed else value.speaker,
        speaker_reviewed=previous.speaker_reviewed,
        text=previous.text if previous.text_reviewed else value.text,
        uncertainty=previous.uncertainty
        if previous.text_reviewed
        else value.uncertainty,
        text_reviewed=previous.text_reviewed,
    )


def stitch(
    previous: TranscriptVersionDTO | None,
    *,
    chunk: ChunkUpdate,
    coverage: tuple[CoverageSlot, ...],
    recording_closed: bool,
) -> TranscriptVersionDTO:
    """Replace only one chunk's hypothesis; never deduplicate equal text."""
    if previous is not None and previous.state == "final":
        raise TranscriptError(TranscriptReason.FINAL)
    chunk_id, segments, complete = chunk.chunk_id, chunk.segments, chunk.complete
    if (
        not any(slot.chunk_id == chunk_id for slot in coverage)
        or len(segments) > MAX_SEGMENTS
        or any(value.chunk_id != chunk_id for value in segments)
        or len({value.segment_id for value in segments}) != len(segments)
    ):
        raise TranscriptError
    retained = (
        tuple(value for value in previous.segments if value.chunk_id != chunk_id)
        if previous is not None
        else ()
    )
    reviewed = (
        {value.segment_id: value for value in previous.segments}
        if previous is not None
        else {}
    )
    segments = tuple(
        _carry_review(value, reviewed.get(value.segment_id)) for value in segments
    )
    combined = tuple(
        sorted(
            (*retained, *segments),
            key=lambda value: (
                value.start_ms,
                value.end_ms,
                value.chunk_id.int,
                value.segment_id.int,
            ),
        )
    )
    if len(combined) > MAX_SEGMENTS:
        raise TranscriptError
    completed = previous.completed_chunks if previous is not None else frozenset()
    completed = completed | {chunk_id} if complete else completed - {chunk_id}
    return TranscriptVersionDTO(
        1 if previous is None else previous.revision + 1,
        "partial",
        combined,
        coverage,
        frozenset(completed),
        recording_closed,
    )


def finalize(
    previous: TranscriptVersionDTO,
    *,
    acknowledge_incomplete: bool,
) -> TranscriptVersionDTO:
    """Require an explicit clinician disposition for incomplete coverage."""
    if not previous.recording_closed:
        raise TranscriptError(TranscriptReason.OPEN)
    if previous.state == "final":
        raise TranscriptError(TranscriptReason.FINAL)
    if (previous.missing or not previous.coverage) and not acknowledge_incomplete:
        raise TranscriptError(TranscriptReason.INCOMPLETE)
    return replace(
        previous,
        revision=previous.revision + 1,
        state="final",
        incomplete_acknowledged=bool(previous.missing or not previous.coverage),
    )


def revise(
    previous: TranscriptVersionDTO,
    *,
    segment_id: UUID,
    speaker: str | None = None,
    text: str | None = None,
    uncertainty: tuple[Uncertainty, ...] | None = None,
) -> TranscriptVersionDTO:
    """Review creates a successor retaining all source identity and history."""
    original = next(
        (value for value in previous.segments if value.segment_id == segment_id),
        None,
    )
    if original is None or (speaker is None and text is None):
        raise TranscriptError
    if text is not None and uncertainty is None:
        raise TranscriptError
    corrected = replace(
        original,
        speaker=original.speaker if speaker is None else speaker,
        text=original.text if text is None else text,
        uncertainty=original.uncertainty if uncertainty is None else uncertainty,
        speaker_reviewed=original.speaker_reviewed or speaker is not None,
        text_reviewed=original.text_reviewed or text is not None,
    )
    return replace(
        previous,
        revision=previous.revision + 1,
        segments=tuple(
            corrected if value.segment_id == segment_id else value
            for value in previous.segments
        ),
    )
