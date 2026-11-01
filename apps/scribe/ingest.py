"""Verified, serialized ingest; no network call runs in the tenant transaction."""

from __future__ import annotations

import secrets
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.core.integration import ActionOperationRequest, enqueue_operation
from apps.providers.services import current_version
from apps.scribe.authority import recording_scope, token_scope
from apps.scribe.contracts import (
    BUFFER_MS,
    SLICE_MS,
    ChunkInput,
    ChunkResult,
    Reason,
    ScribeError,
)
from apps.scribe.controls import update_gaps
from apps.scribe.models import AudioChunk, CaptureEpoch, RecordingSession
from apps.scribe.storage import media_location, media_storage, seal_audio

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


def utc_now() -> datetime:
    """Clock seam for deadline tests; reconnect never changes a slice deadline."""
    return timezone.now()


def _repeated_chunk(
    *, previous: AudioChunk, chunk: ChunkInput, epoch: CaptureEpoch, clinic_id: UUID
) -> ChunkResult:
    """Converge identical terms or quarantine a conflicting identity."""
    if (
        previous.sha256 == chunk.sha256
        and previous.size_bytes == len(chunk.data)
        and previous.duration_ms == chunk.duration_ms
        and previous.codec == chunk.codec
        and previous.state in {"buffered", "accepted"}
    ):
        return ChunkResult("duplicate")
    if previous.state in {"buffered", "accepted"}:
        previous.state = "quarantined"
        previous.wrapped_key = b""
        previous.encrypted_data = b""
        previous.save(update_fields=("state", "wrapped_key", "encrypted_data"))
        update_gaps(epoch, track=chunk.track, last_seq=chunk.seq)
        record_phase1_event(
            "scribe.chunk.quarantined",
            clinic_id=clinic_id,
            affected_record_id=previous.pk,
        )
    return ChunkResult("rejected", "conflicting_duplicate")


def ingest_chunk(
    *, clinic_id: UUID, encounter_token: str, chunk: ChunkInput
) -> ChunkResult:
    """Acknowledge only committed encrypted bytes, reject stale authority first."""
    chunk.validate()
    with transaction.atomic():
        scope = token_scope(clinic_id=clinic_id, token=encounter_token)
        try:
            recording, _scope = recording_scope(
                clinic_id=clinic_id, session_id=chunk.session_id
            )
        except ScribeError as error:
            if error.code == "consent_revoked":
                return ChunkResult("rejected", "consent_revoked")
            raise
        if (
            recording.encounter_id != scope.encounter.pk
            or recording.token_digest != sha256(encounter_token.encode()).hexdigest()
        ):
            raise ScribeError
        recording = RecordingSession.objects.select_for_update().get(pk=recording.pk)
        if recording.state != "recording" or scope.encounter.state != "open":
            return ChunkResult("rejected", "session_not_recording")
        epoch = CaptureEpoch.objects.filter(
            pk=chunk.epoch, session=recording, number=recording.epoch
        ).first()
        if epoch is None:
            return ChunkResult("rejected", "epoch_stale")
        captured_at = epoch.started_at + timedelta(milliseconds=chunk.seq * SLICE_MS)
        expires_at = captured_at + timedelta(milliseconds=BUFFER_MS)
        version = current_version("object_storage_media", clinic_id=clinic_id)
        if version is None or version.pk != recording.capability_version_id:
            raise ScribeError(Reason.STORAGE_UNAVAILABLE)
        media_storage(clinic_id=clinic_id)
        previous = AudioChunk.objects.filter(
            session=recording, epoch=epoch, track=chunk.track, seq=chunk.seq
        ).first()
        if previous is not None:
            return _repeated_chunk(
                previous=previous, chunk=chunk, epoch=epoch, clinic_id=clinic_id
            )
        if utc_now() >= expires_at:
            return ChunkResult("rejected", "chunk_expired")
        if captured_at > utc_now() + timedelta(seconds=5):
            raise ScribeError(Reason.INVALID_CHUNK)
        key = secrets.token_hex(32)
        sealed = seal_audio(key=key, data=chunk.data)
        stored = AudioChunk.objects.create(
            organization_id=recording.organization_id,
            session=recording,
            epoch=epoch,
            track=chunk.track,
            seq=chunk.seq,
            sha256=chunk.sha256,
            size_bytes=len(chunk.data),
            duration_ms=chunk.duration_ms,
            codec=chunk.codec,
            storage_key=key,
            storage_location=media_location(),
            expires_at=expires_at,
            wrapped_key=sealed.wrapped_key,
            encrypted_data=sealed.ciphertext,
        )
        operation = enqueue_operation(
            ActionOperationRequest(
                provider="scribe_media",
                clinic_id=clinic_id,
                subject_type="scribe.chunk",
                subject_id=stored.pk,
                idempotency_key=stored.pk,
                payload_digest=stored.sha256,
            )
        )
        stored.operation_id = operation
        stored.save(update_fields=("operation_id",))
        update_gaps(epoch, track=chunk.track, last_seq=chunk.seq)
        record_phase1_event(
            "scribe.chunk.received", clinic_id=clinic_id, affected_record_id=stored.pk
        )
        return ChunkResult("received")
