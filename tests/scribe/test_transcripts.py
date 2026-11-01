"""Database-free transcript ordering, coverage, uncertainty and adapter proofs."""

from __future__ import annotations

import json
from dataclasses import replace
from itertools import permutations
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.scribe.asr import (
    AudioReference,
    AWSAdapter,
    AzureAdapter,
    GoogleAdapter,
    RecordedASRAdapter,
    decode_reference,
    parse_batch,
)
from apps.scribe.transcript_contracts import (
    ChunkUpdate,
    CoverageSlot,
    Segment,
    TranscriptError,
    Uncertainty,
    finalize,
    revise,
    stitch,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.scribe.asr import ASRAdapter
    from apps.scribe.transcript_contracts import TranscriptVersionDTO


def identifier(number: int) -> UUID:
    return UUID(int=number)


def slots(*, missing: bool = False) -> tuple[CoverageSlot, ...]:
    return tuple(
        CoverageSlot(
            identifier(100),
            "microphone",
            index,
            None if missing and index == 2 else identifier(index + 1),
        )
        for index in range(4)
    )


def segment(index: int, text: str = "sim sim") -> Segment:
    return Segment(
        identifier(200 + index),
        identifier(index + 1),
        index * 2000,
        index * 2000 + 1000,
        text,
    )


def visit(order: tuple[int, ...] = (0, 1, 2, 3)) -> TranscriptVersionDTO:
    current = None
    for index in order:
        current = stitch(
            current,
            chunk=ChunkUpdate(identifier(index + 1), (segment(index),), complete=True),
            coverage=slots(),
            recording_closed=True,
        )
    assert current is not None
    return current


@pytest.mark.parametrize("order", list(permutations(range(4))))
def test_all_24_chunk_reorderings_stitch_by_time_without_text_dedup(
    order: tuple[int, ...],
) -> None:
    recorded = tuple(
        replace(
            parse_batch(
                RecordedASRAdapter().transcribe(duration_ms=2000, fixture_index=index),
                duration_ms=2000,
            )[-1].segments[0],
            chunk_id=identifier(index + 1),
            segment_id=identifier(200 + index),
            start_ms=index * 2000,
            end_ms=(index + 1) * 2000,
        )
        for index in range(4)
    )
    current = None
    for index in order:
        current = stitch(
            current,
            chunk=ChunkUpdate(identifier(index + 1), (recorded[index],), complete=True),
            coverage=slots(),
            recording_closed=True,
        )
    assert current is not None
    result = finalize(current, acknowledge_incomplete=False)
    assert result.state == "final"
    assert result.segments == recorded
    assert result.segments[0].text == "sim sim"
    assert result.segments[2].uncertainty
    assert result.missing == ()


def test_gaps_remain_explicit_until_an_actual_recognition_arrives() -> None:
    current = stitch(
        None,
        chunk=ChunkUpdate(identifier(1), (segment(0),), complete=True),
        coverage=slots(missing=True),
        recording_closed=True,
    )
    assert len(current.missing) == 3
    assert current.segments == (segment(0),)
    with pytest.raises(TranscriptError, match="coverage_incomplete"):
        finalize(current, acknowledge_incomplete=False)
    acknowledged = finalize(current, acknowledge_incomplete=True)
    assert acknowledged.incomplete_acknowledged is True
    assert acknowledged.missing == current.missing
    assert acknowledged.segments == current.segments


def test_live_recording_cannot_be_finalized_even_with_an_acknowledgment() -> None:
    with pytest.raises(TranscriptError, match="recording_open"):
        finalize(
            replace(visit(), recording_closed=False),
            acknowledge_incomplete=True,
        )


def test_silent_recognized_chunk_counts_as_coverage_without_inventing_text() -> None:
    current = stitch(
        None,
        chunk=ChunkUpdate(identifier(1), (), complete=True),
        coverage=slots()[:1],
        recording_closed=True,
    )
    assert finalize(current, acknowledge_incomplete=False).segments == ()


def test_partial_is_replaced_by_source_identity_not_repeated_words() -> None:
    first = stitch(
        None,
        chunk=ChunkUpdate(identifier(1), (segment(0, "5"),), complete=False),
        coverage=slots()[:1],
        recording_closed=True,
    )
    assert first.missing == slots()[:1]
    final_segment = replace(
        segment(0, "5 ou 15 mg"),
        uncertainty=(Uncertainty(0, 10, "uncertain"),),
        confidence_milli=420,
    )
    second = stitch(
        first,
        chunk=ChunkUpdate(identifier(1), (final_segment,), complete=True),
        coverage=slots()[:1],
        recording_closed=True,
    )
    assert first.segments[0].text == "5"
    assert second.revision == first.revision + 1
    assert second.segments == (final_segment,)
    assert second.missing == ()


def test_speaker_review_and_corrections_create_immutable_successors() -> None:
    original = finalize(visit(), acknowledge_incomplete=False)
    relabeled = revise(original, segment_id=identifier(201), speaker="caregiver")
    corrected = revise(
        relabeled,
        segment_id=identifier(201),
        text="SINTETICO-SENTINELA-CORRECTED",
        uncertainty=(),
    )
    assert original.segments[1].speaker == "unknown"
    assert relabeled.segments[1].speaker == "caregiver"
    assert corrected.state == "final"
    assert corrected.revision == original.revision + 2
    assert relabeled.segments[1].text == "sim sim"
    assert "SINTETICO-SENTINELA-CORRECTED" not in repr(corrected)


def test_second_voice_is_not_assumed_to_be_the_patient() -> None:
    result = visit()
    assert all(value.speaker == "unknown" for value in result.segments)
    with pytest.raises(TranscriptError, match="invalid_transcript"):
        revise(result, segment_id=identifier(201), speaker="second_voice")


def test_late_model_completion_never_overwrites_a_final() -> None:
    original = finalize(visit(), acknowledge_incomplete=False)
    with pytest.raises(TranscriptError, match="transcript_final"):
        stitch(
            original,
            chunk=ChunkUpdate(identifier(1), (segment(0, "late"),), complete=True),
            coverage=slots(),
            recording_closed=True,
        )


def test_human_review_survives_a_later_partial_to_final_replacement() -> None:
    original = stitch(
        None,
        chunk=ChunkUpdate(identifier(1), (segment(0),), complete=False),
        coverage=slots()[:1],
        recording_closed=False,
    )
    reviewed = revise(original, segment_id=identifier(200), speaker="caregiver")
    reviewed = revise(
        reviewed,
        segment_id=identifier(200),
        text="revisado",
        uncertainty=(),
    )
    late = stitch(
        reviewed,
        chunk=ChunkUpdate(identifier(1), (segment(0, "late"),), complete=True),
        coverage=slots()[:1],
        recording_closed=False,
    )
    assert late.segments[0].text == "revisado"
    assert late.segments[0].speaker == "caregiver"
    assert late.segments[0].text_reviewed
    assert late.segments[0].speaker_reviewed


def test_review_is_not_reassigned_to_a_different_source_time_range() -> None:
    original = stitch(
        None,
        chunk=ChunkUpdate(identifier(1), (segment(0),), complete=False),
        coverage=slots()[:1],
        recording_closed=False,
    )
    reviewed = revise(original, segment_id=identifier(200), speaker="caregiver")
    moved = replace(segment(0, "new source interval"), start_ms=1000, end_ms=2000)
    late = stitch(
        reviewed,
        chunk=ChunkUpdate(identifier(1), (moved,), complete=True),
        coverage=slots()[:1],
        recording_closed=False,
    )
    assert reviewed.segments[0].speaker == "caregiver"
    assert late.segments[0].speaker == "unknown"
    assert not late.segments[0].speaker_reviewed


def test_azure_partial_events_have_unknown_confidence_and_explicit_uncertainty() -> (
    None
):
    result = parse_batch(
        AzureAdapter().normalize_partial(
            text="5 ou",
            offset_ticks=0,
            duration_ticks=10_000_000,
            duration_ms=2000,
        ),
        duration_ms=2000,
    )[0]
    assert not result.is_final
    assert result.segments[0].confidence_milli is None
    assert result.segments[0].uncertainty == (Uncertainty(0, 4, "uncertain"),)


def test_durable_asr_reference_contains_only_ciphertext_not_a_raw_audio_copy() -> None:
    reference = AudioReference(
        identifier(1),
        identifier(2),
        "audio/webm;codecs=opus",
        2000,
        b"synthetic-object-ciphertext",
    )
    encoded = reference.encode()
    assert json.loads(encoded)["schema"] == "scribe.audio-reference.v1"
    assert "audio_b64" not in json.loads(encoded)
    assert "fixture_index" not in json.loads(encoded)
    assert decode_reference(encoded) == reference
    assert "synthetic-object-ciphertext" not in repr(reference)


@pytest.mark.parametrize(
    "construct",
    [
        lambda: replace(segment(0), start_ms=-1),
        lambda: replace(segment(0), end_ms=0),
        lambda: replace(segment(0), start_ms=True),
        lambda: replace(segment(0), confidence_milli=1001),
        lambda: replace(segment(0), speaker="relative"),
        lambda: replace(segment(0), language="../../private"),
        lambda: replace(segment(0), uncertainty=(Uncertainty(0, 99, "uncertain"),)),
    ],
)
def test_untrusted_segment_terms_are_bounded(construct: Callable[[], Segment]) -> None:
    with pytest.raises(TranscriptError, match="invalid_transcript"):
        construct()


def test_fixture_dose_ambiguity_has_a_reviewable_uncertainty_span() -> None:
    batch = RecordedASRAdapter().transcribe(
        duration_ms=2000,
        fixture_index=2,
    )
    partial, final = parse_batch(batch, duration_ms=2000)
    assert partial.is_final is False
    assert final.is_final is True
    value = final.segments[0]
    assert value.text == "5 ou 15 mg"
    assert value.uncertainty == (Uncertainty(0, 10, "uncertain"),)
    assert value.speaker == "unknown"
    assert value.text not in repr(value)


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"schema_version":1,"events":[]}',
        '{"schema_version":1,"events":[],"tool":"execute"}',
        '{"schema_version":1,"events":[{"is_final":true,"segments":[{"text":"hidden"}]}]}',
    ],
)
def test_provider_output_rejects_open_and_ambiguous_schemas(payload: str) -> None:
    with pytest.raises(TranscriptError, match="invalid_transcript"):
        parse_batch(payload, duration_ms=2000)


@pytest.mark.parametrize("adapter", [AWSAdapter(), AzureAdapter(), GoogleAdapter()])
def test_real_provider_execution_is_blocked_before_any_network(
    adapter: ASRAdapter,
) -> None:
    assert adapter.real_enabled is False
    with pytest.raises(TranscriptError, match="blocked_on_eg"):
        adapter.transcribe(duration_ms=2000, fixture_index=0)


def test_aws_partial_speaker_result_normalizes_without_role_inference() -> None:
    batch = AWSAdapter().normalize(
        {
            "Transcript": {
                "Results": [
                    {
                        "ResultId": "synthetic-result",
                        "IsPartial": True,
                        "StartTime": 0,
                        "EndTime": 1,
                        "Alternatives": [
                            {
                                "Transcript": "sim sim",
                                "Items": [
                                    {
                                        "Content": "sim",
                                        "Speaker": "1",
                                        "Confidence": 0.4,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        },
        duration_ms=2000,
    )
    result = parse_batch(batch, duration_ms=2000)[0]
    assert not result.is_final
    assert result.segments[0].speaker == "unknown"
    assert result.segments[0].confidence_milli == 400


def test_azure_documented_detailed_result_preserves_text_and_timing() -> None:
    batch = AzureAdapter().normalize(
        {
            "RecognitionStatus": "Success",
            "Offset": 1_000_000,
            "Duration": 10_000_000,
            "SpeakerId": "Guest-2",
            "NBest": [{"Confidence": 0.4, "Display": "5 ou 15 mg"}],
        },
        duration_ms=2000,
    )
    result = parse_batch(batch, duration_ms=2000)[0]
    assert result.is_final
    assert result.segments[0].start_ms == 100
    assert result.segments[0].end_ms == 1100
    assert result.segments[0].speaker == "unknown"
    assert result.segments[0].uncertainty


def test_google_documented_word_offsets_never_remove_repeated_words() -> None:
    batch = GoogleAdapter().normalize(
        {
            "results": [
                {
                    "languageCode": "pt-BR",
                    "resultEndOffset": "1.2s",
                    "alternatives": [
                        {
                            "transcript": "sim sim",
                            "confidence": 0.8,
                            "words": [
                                {
                                    "word": "sim",
                                    "startOffset": "0.1s",
                                    "endOffset": "1.2s",
                                    "speakerLabel": "2",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        duration_ms=2000,
    )
    value = parse_batch(batch, duration_ms=2000)[0].segments[0]
    assert value.text == "sim sim"
    assert value.start_ms == 100
    assert value.end_ms == 1200
    assert value.speaker == "unknown"
    assert json.loads(batch)["schema_version"] == 1
