"""Bounded commands and content-free results at the recording boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime

SLICE_MS: Final = 2000
BUFFER_MS: Final = 60000
MAX_CHUNK_BYTES: Final = 256 * 1024
MAX_SEQUENCE: Final = 10799
MAX_DURATION_MS: Final = 5000
CODECS: Final = ("audio/webm;codecs=opus", "audio/ogg;codecs=opus")
TRACKS: Final = ("microphone", "remote")
LIVE_STATES: Final = ("created", "authorized", "recording", "paused")
type ControlAction = Literal["start", "pause", "resume", "stop", "cancel", "recover"]
type GapReason = Literal["user", "offline", "microphone", "patient_switch", "recovery"]


class Reason(StrEnum):
    """The complete non-identifying refusal vocabulary."""

    ACCESS_DENIED = "access_denied"
    INVALID_CHUNK = "invalid_chunk"
    INVALID_COMMAND = "invalid_command"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    AUDIO_UNAVAILABLE = "audio_unavailable"
    AUDIO_PURGED = "audio_purged"
    CONSENT_REVOKED = "consent_revoked"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    ALREADY_ACTIVE = "recording_already_active"
    SESSION_NOT_RECORDING = "session_not_recording"
    EPOCH_STALE = "epoch_stale"
    CHUNK_EXPIRED = "chunk_expired"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"


class ScribeError(Exception):
    """Expose only a closed, non-identifying refusal code."""

    def __init__(self, code: Reason = Reason.ACCESS_DENIED) -> None:
        """Retain a machine result without content or provider exception text."""
        self.code = code
        super().__init__(code)


class AudioPurgedError(ScribeError):
    """Expose the policy receipt date only after the caller's recording authority."""

    def __init__(self, *, receipt_date: datetime) -> None:
        """Keep a fixed reason rather than an object key or clinical identifier."""
        self.receipt_date = receipt_date
        super().__init__(Reason.AUDIO_PURGED)


@dataclass(frozen=True, slots=True)
class ChunkInput:
    """One untrusted slice; validate before any durable write."""

    session_id: UUID
    epoch: UUID
    track: str
    seq: int
    sha256: str
    duration_ms: int
    codec: str
    data: bytes = field(repr=False)

    def validate(self) -> None:
        """Reject oversized, corrupt and non-Opus slices at the input boundary."""
        if (
            not isinstance(self.epoch, UUID)
            or type(self.seq) is not int
            or not 0 <= self.seq <= MAX_SEQUENCE
            or type(self.duration_ms) is not int
            or not 1 <= self.duration_ms <= MAX_DURATION_MS
            or self.codec not in CODECS
            or self.track not in TRACKS
            or not isinstance(self.data, bytes)
            or not 1 <= len(self.data) <= MAX_CHUNK_BYTES
            or re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None
            or sha256(self.data).hexdigest() != self.sha256
        ):
            raise ScribeError(Reason.INVALID_CHUNK)


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """A receipt confirms durable encrypted ingest, not transcription."""

    ack: Literal["received", "duplicate", "rejected"]
    code: str = ""


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Stable command receipt; retries never issue another capture epoch."""

    session_id: UUID
    state: str
    epoch: int
