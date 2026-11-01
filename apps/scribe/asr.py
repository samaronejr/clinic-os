"""Offline ASR plus documented vendor codecs; all vendor execution stays gated."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Protocol, cast
from uuid import UUID

from apps.scribe.contracts import CODECS, MAX_CHUNK_BYTES, MAX_DURATION_MS
from apps.scribe.transcript_contracts import (
    MAX_TEXT,
    MAX_TIMELINE_MS,
    Segment,
    TranscriptError,
    TranscriptReason,
    Uncertainty,
)

MAX_EVENTS: Final = 4
MAX_CHUNK_SEGMENTS: Final = 128
MAX_RESPONSE_BYTES: Final = 262_144
MAX_BASE64_BYTES: Final = 4 * ((MAX_CHUNK_BYTES + 2) // 3)
MAX_CIPHERTEXT_BYTES: Final = MAX_CHUNK_BYTES + 8192
FIXTURE_COUNT: Final = 4
UNCERTAIN_CONFIDENCE: Final = 700


@dataclass(frozen=True, slots=True)
class Recognition:
    """One replacement hypothesis over the same bounded chunk time range."""

    is_final: bool
    segments: tuple[Segment, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class AudioInput:
    """Transient plaintext bound to an accepted chunk and exact AI consent."""

    chunk_id: UUID
    consent_id: UUID
    codec: str
    duration_ms: int
    audio: bytes = field(repr=False)

    def encode(self) -> str:
        """Produce ephemeral provider input, never a durable artifact or broker row."""
        return json.dumps(
            {
                "schema": "scribe.audio.v1",
                "chunk_id": str(self.chunk_id),
                "consent_id": str(self.consent_id),
                "codec": self.codec,
                "duration_ms": self.duration_ms,
                "audio_b64": base64.b64encode(self.audio).decode("ascii"),
            }
        )


@dataclass(frozen=True, slots=True)
class AudioReference:
    """Durable input retains ciphertext, never a second decryptable audio copy."""

    chunk_id: UUID
    consent_id: UUID
    codec: str
    duration_ms: int
    ciphertext: bytes = field(repr=False)

    def encode(self) -> str:
        """Keep durable input under the chunk's sole wrapped object key."""
        return json.dumps(
            {
                "schema": "scribe.audio-reference.v1",
                "chunk_id": str(self.chunk_id),
                "consent_id": str(self.consent_id),
                "codec": self.codec,
                "duration_ms": self.duration_ms,
                "ciphertext_b64": base64.b64encode(self.ciphertext).decode("ascii"),
            }
        )


def decode_reference(content: str) -> AudioReference:
    """Decode only the immutable reference envelope, with no key material."""
    try:
        payload = _object(json.loads(content, object_pairs_hook=_unique))
        if (
            set(payload)
            != {
                "schema",
                "chunk_id",
                "consent_id",
                "codec",
                "duration_ms",
                "ciphertext_b64",
            }
            or payload["schema"] != "scribe.audio-reference.v1"
        ):
            raise TranscriptError
        encoded = payload["ciphertext_b64"]
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= (
            4 * ((MAX_CIPHERTEXT_BYTES + 2) // 3)
        ):
            raise TranscriptError
        ciphertext = base64.b64decode(encoded, validate=True)
        value = AudioReference(
            UUID(_text(payload["chunk_id"])),
            UUID(_text(payload["consent_id"])),
            _text(payload["codec"]),
            _integer(payload["duration_ms"]),
            ciphertext,
        )
        if (
            not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES
            or value.codec not in CODECS
            or not 1 <= value.duration_ms <= MAX_DURATION_MS
        ):
            raise TranscriptError
    except (ValueError, binascii.Error, RecursionError) as error:
        raise TranscriptError from error
    return value


def decode_audio(content: str) -> AudioInput:
    """Reject forged/open audio envelopes before any provider or source access."""
    try:
        payload = _object(json.loads(content, object_pairs_hook=_unique))
        if (
            set(payload)
            != {
                "schema",
                "chunk_id",
                "consent_id",
                "codec",
                "duration_ms",
                "audio_b64",
            }
            or payload["schema"] != "scribe.audio.v1"
        ):
            raise TranscriptError
        encoded = payload["audio_b64"]
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_BASE64_BYTES:
            raise TranscriptError
        audio = base64.b64decode(encoded, validate=True)
        value = AudioInput(
            UUID(_text(payload["chunk_id"])),
            UUID(_text(payload["consent_id"])),
            _text(payload["codec"]),
            _integer(payload["duration_ms"]),
            audio,
        )
        if (
            not 1 <= len(audio) <= MAX_CHUNK_BYTES
            or value.codec not in CODECS
            or not 1 <= value.duration_ms <= MAX_DURATION_MS
        ):
            raise TranscriptError
    except (ValueError, binascii.Error, RecursionError) as error:
        raise TranscriptError from error
    return value


class ASRAdapter(Protocol):
    """Synthetic adapters run locally; vendor transports require external gates."""

    @property
    def real_enabled(self) -> bool:
        """Remain read-only so a caller cannot enable a provider."""
        ...

    def transcribe(self, *, duration_ms: int, fixture_index: int) -> str:
        """Return the closed, versioned recognition event envelope."""
        ...


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise TranscriptError
    return cast("dict[str, object]", value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > MAX_CHUNK_SEGMENTS:
        raise TranscriptError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TEXT:
        raise TranscriptError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TranscriptError
    return value


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TranscriptError
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise TranscriptError from error
    if not parsed.is_finite() or parsed < 0 or parsed > MAX_TIMELINE_MS:
        raise TranscriptError
    return parsed


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TranscriptError
        result[key] = value
    return result


def _parse_segment(raw: object, *, duration_ms: int, index: int) -> Segment:
    value = _object(raw)
    if set(value) != {
        "start_ms",
        "end_ms",
        "text",
        "speaker",
        "confidence_milli",
        "uncertainty",
        "language",
    }:
        raise TranscriptError
    spans: list[Uncertainty] = []
    for raw_span in _array(value["uncertainty"]):
        span = _object(raw_span)
        if set(span) != {"start", "end", "kind"} or span["kind"] not in {
            "uncertain",
            "inaudible",
        }:
            raise TranscriptError
        spans.append(
            Uncertainty(
                _integer(span["start"]),
                _integer(span["end"]),
                span["kind"],
            )
        )
    confidence = value["confidence_milli"]
    item = Segment(
        UUID(int=index + 1),
        UUID(int=0),
        _integer(value["start_ms"]),
        _integer(value["end_ms"]),
        _text(value["text"]),
        _text(value["speaker"]),
        None if confidence is None else _integer(confidence),
        tuple(spans),
        _text(value["language"]),
    )
    if item.end_ms > duration_ms:
        raise TranscriptError
    return item


def parse_batch(content: str, *, duration_ms: int) -> tuple[Recognition, ...]:
    """Validate untrusted output, including exact keys and chunk-local timing."""
    if (
        not isinstance(content, str)
        or len(content.encode("utf-8")) > MAX_RESPONSE_BYTES
        or type(duration_ms) is not int
        or not 1 <= duration_ms <= MAX_DURATION_MS
    ):
        raise TranscriptError
    try:
        raw: object = json.loads(content, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as error:
        raise TranscriptError from error
    payload = _object(raw)
    if set(payload) != {"schema_version", "events"} or (
        type(payload["schema_version"]) is not int or payload["schema_version"] != 1
    ):
        raise TranscriptError
    events = _array(payload["events"])
    if not 1 <= len(events) <= MAX_EVENTS:
        raise TranscriptError
    results: list[Recognition] = []
    for raw_event in events:
        event = _object(raw_event)
        if (
            set(event) != {"is_final", "segments"}
            or type(event["is_final"]) is not bool
        ):
            raise TranscriptError
        segments = tuple(
            _parse_segment(raw, duration_ms=duration_ms, index=index)
            for index, raw in enumerate(_array(event["segments"]))
        )
        if results and results[-1].is_final:
            raise TranscriptError
        results.append(Recognition(event["is_final"], segments))
    return tuple(results)


def _segment(
    *,
    text: str,
    start: int,
    end: int,
    confidence: object,
    language: str = "pt-BR",
) -> dict[str, object]:
    milli = None if confidence is None else int(_decimal(confidence) * 1000)
    spans = (
        [{"start": 0, "end": len(text), "kind": "uncertain"}]
        if milli is None or milli < UNCERTAIN_CONFIDENCE
        else []
    )
    return {
        "start_ms": start,
        "end_ms": end,
        "text": text,
        "speaker": "unknown",
        "confidence_milli": milli,
        "uncertainty": spans,
        "language": language,
    }


def _batch(events: list[dict[str, object]], duration_ms: int) -> str:
    content = json.dumps(
        {"schema_version": 1, "events": events},
        ensure_ascii=False,
        allow_nan=False,
    )
    parse_batch(content, duration_ms=duration_ms)
    return content


class RecordedASRAdapter:
    """Code-versioned synthetic visit; no audio recognition or network is claimed."""

    real_enabled: Final = False

    def transcribe(self, *, duration_ms: int, fixture_index: int) -> str:
        """Replay a partial then its final, including inaudibility/dose ambiguity."""
        if type(fixture_index) is not int or not 0 <= fixture_index < FIXTURE_COUNT:
            raise TranscriptError
        payload = _object(
            json.loads(
                (Path(__file__).parent / "fixtures" / "asr_v1.json").read_text(
                    encoding="utf-8"
                ),
                object_pairs_hook=_unique,
            )
        )
        if (
            set(payload) != {"schema_version", "data_class", "records"}
            or payload["schema_version"] != 1
            or payload["data_class"] != "synthetic"
        ):
            raise TranscriptError
        records = _array(payload["records"])
        if len(records) != FIXTURE_COUNT:
            raise TranscriptError
        record = _object(records[fixture_index])
        if (
            set(record) != {"text", "confidence", "inaudible"}
            or type(record["inaudible"]) is not bool
        ):
            raise TranscriptError
        text = _text(record["text"])
        confidence = record["confidence"]
        partial = _segment(
            text=text[: max(1, len(text) // 2)],
            start=0,
            end=duration_ms,
            confidence=None,
        )
        final = _segment(
            text=text,
            start=0,
            end=duration_ms,
            confidence=confidence,
        )
        if record["inaudible"]:
            final["uncertainty"] = [{"start": 0, "end": len(text), "kind": "inaudible"}]
        return _batch(
            [
                {"is_final": False, "segments": [partial]},
                {"is_final": True, "segments": [final]},
            ],
            duration_ms,
        )


def recorded_gateway_result(content: str) -> str | None:
    """Recognize only the encrypted audio envelope, not an arbitrary file/URL."""
    try:
        raw: object = json.loads(content, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != "scribe.audio.v1":
        return None
    payload = decode_audio(content)
    fixture_index = 0
    for index in range(FIXTURE_COUNT):
        if payload.audio == f"SINTETICO-ASR-FIXTURE:{index}".encode("ascii"):
            fixture_index = index
            break
    return RecordedASRAdapter().transcribe(
        duration_ms=payload.duration_ms,
        fixture_index=fixture_index,
    )


class _BlockedVendor:
    """Response codecs are executable offline; vendor inference is unreachable."""

    real_enabled: Final = False

    def transcribe(self, *, duration_ms: int, fixture_index: int) -> str:
        """Refuse before credentials, SDK construction, DNS, or an HTTP request."""
        del duration_ms, fixture_index
        raise TranscriptError(TranscriptReason.BLOCKED)


class AWSAdapter(_BlockedVendor):
    """AWS Transcribe Streaming TranscriptEvent response codec."""

    @staticmethod
    def request_options(*, codec: str) -> dict[str, object]:
        """AWS streaming accepts Ogg Opus, not the browser's WebM container."""
        if codec != "audio/ogg;codecs=opus":
            raise TranscriptError
        return {
            "LanguageCode": "pt-BR",
            "MediaEncoding": "ogg-opus",
            "MediaSampleRateHertz": 48000,
            "ShowSpeakerLabel": True,
            "EnablePartialResultsStabilization": True,
        }

    def normalize(self, payload: object, *, duration_ms: int) -> str:
        """Preserve partial/final flags and time ranges; do not infer roles."""
        transcript = _object(_object(payload).get("Transcript"))
        segments: list[dict[str, object]] = []
        final = True
        for raw in _array(transcript.get("Results")):
            result = _object(raw)
            alternatives = _array(result.get("Alternatives"))
            if not alternatives or type(result.get("IsPartial")) is not bool:
                raise TranscriptError
            alternative = _object(alternatives[0])
            confidences = [
                _decimal(_object(item)["Confidence"])
                for item in _array(alternative.get("Items", []))
                if _object(item).get("Confidence") is not None
            ]
            final = final and not result["IsPartial"]
            segments.append(
                _segment(
                    text=_text(alternative.get("Transcript")),
                    start=int(_decimal(result.get("StartTime")) * 1000),
                    end=int(_decimal(result.get("EndTime")) * 1000),
                    confidence=str(min(confidences)) if confidences else None,
                )
            )
        return _batch([{"is_final": final, "segments": segments}], duration_ms)


class AzureAdapter(_BlockedVendor):
    """Azure Speech detailed recognition result codec (100 ns ticks)."""

    @staticmethod
    def request_options() -> dict[str, object]:
        """Compressed Opus additionally needs a qualified SDK/GStreamer path."""
        return {
            "speech_recognition_language": "pt-BR",
            "output_format": "Detailed",
            "request_word_level_timestamps": True,
        }

    def normalize_partial(
        self,
        *,
        text: str,
        offset_ticks: int,
        duration_ticks: int,
        duration_ms: int,
    ) -> str:
        """Map SDK Recognizing event properties without inventing confidence."""
        start = int(_decimal(offset_ticks) / 10_000)
        return _batch(
            [
                {
                    "is_final": False,
                    "segments": [
                        _segment(
                            text=_text(text),
                            start=start,
                            end=start + int(_decimal(duration_ticks) / 10_000),
                            confidence=None,
                        )
                    ],
                }
            ],
            duration_ms,
        )

    def normalize(self, payload: object, *, duration_ms: int) -> str:
        """Only successful detailed finals carry confidence and word timing."""
        result = _object(payload)
        if result.get("RecognitionStatus") != "Success":
            raise TranscriptError
        alternatives = _array(result.get("NBest"))
        if not alternatives:
            raise TranscriptError
        alternative = _object(alternatives[0])
        start = int(_decimal(result.get("Offset")) / 10_000)
        duration = int(_decimal(result.get("Duration")) / 10_000)
        return _batch(
            [
                {
                    "is_final": True,
                    "segments": [
                        _segment(
                            text=_text(alternative.get("Display")),
                            start=start,
                            end=start + duration,
                            confidence=alternative.get("Confidence"),
                        )
                    ],
                }
            ],
            duration_ms,
        )


class GoogleAdapter(_BlockedVendor):
    """Google Speech-to-Text v2 RecognizeResponse/StreamingRecognitionResult codec."""

    @staticmethod
    def request_options() -> dict[str, object]:
        """Use documented container auto-detection and word-level results."""
        return {
            "config": {
                "autoDecodingConfig": {},
                "languageCodes": ["pt-BR"],
                "model": "long",
                "features": {
                    "enableWordTimeOffsets": True,
                    "enableWordConfidence": True,
                    "diarizationConfig": {"minSpeakerCount": 1, "maxSpeakerCount": 6},
                },
            }
        }

    @staticmethod
    def _offset(value: object) -> int:
        if not isinstance(value, str) or not value.endswith("s"):
            raise TranscriptError
        return int(_decimal(value[:-1]) * 1000)

    def normalize(self, payload: object, *, duration_ms: int) -> str:
        """Keep transcript strings verbatim; speaker labels remain proposals."""
        segments: list[dict[str, object]] = []
        final = True
        for raw in _array(_object(payload).get("results")):
            result = _object(raw)
            alternatives = _array(result.get("alternatives"))
            if not alternatives:
                raise TranscriptError
            alternative = _object(alternatives[0])
            words = _array(alternative.get("words", []))
            start = (
                self._offset(_object(words[0]).get("startOffset", "0s")) if words else 0
            )
            end = self._offset(result.get("resultEndOffset"))
            if "isFinal" in result:
                if type(result["isFinal"]) is not bool:
                    raise TranscriptError
                final = final and bool(result["isFinal"])
            segments.append(
                _segment(
                    text=_text(alternative.get("transcript")),
                    start=start,
                    end=end,
                    confidence=alternative.get("confidence"),
                    language=_text(result.get("languageCode", "pt-BR")),
                )
            )
        return _batch([{"is_final": final, "segments": segments}], duration_ms)
