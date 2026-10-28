"""Consent-bound chunk admission and governed-gateway transcript settlement."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.ai.artifacts import create_input
from apps.ai.contracts import Budget, Purpose, Usage
from apps.ai.services import invoke
from apps.audit.services import record_phase1_event
from apps.consent.services import consent_for_future_use
from apps.identity.current_context import CurrentActorError
from apps.scribe.asr import AudioInput, AudioReference, decode_reference, parse_batch
from apps.scribe.authority import (
    active_consent,
    lock_recording_consent,
    recording_scope,
)
from apps.scribe.contracts import SLICE_MS, ScribeError
from apps.scribe.models import AudioChunk, ChunkTranscription, RecordingSession
from apps.scribe.storage import media_storage
from apps.scribe.transcript_contracts import ChunkUpdate, TranscriptError, stitch
from apps.scribe.transcripts import (
    RecognitionReceipt,
    TranscriptAccessDeniedError,
    current_snapshot,
    refresh_coverage,
    save_snapshot,
)
from apps.tenancy.envelope import EnvelopeError, reveal

if TYPE_CHECKING:
    from apps.ai.models import AIArtifact
    from apps.consent.models import ConsentAcceptance
    from apps.ehr.attachment_storage import AttachmentStorage

type TranscriptionStatus = Literal["queued", "transcription_pending", "complete"]


@dataclass(frozen=True, slots=True)
class PreparedAudio:
    """Only ciphertext is fetched outside the tenant transaction."""

    chunk_id: UUID
    key: str = field(repr=False)
    storage: AttachmentStorage = field(repr=False)


def _consented(
    *,
    clinic_id: UUID,
    chunk_id: UUID,
    lock: bool = False,
) -> tuple[AudioChunk, RecordingSession, ConsentAcceptance]:
    try:
        chunk = AudioChunk.objects.filter(pk=chunk_id, state="accepted").first()
        if chunk is None or not chunk.wrapped_key:
            raise TranscriptAccessDeniedError
        recording, scope = recording_scope(
            clinic_id=clinic_id,
            session_id=chunk.session_id,
            consent_required=False,
        )
        if lock:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    [f"consent:{clinic_id}:ai_assistance"],
                )
            lock_recording_consent(clinic_id=clinic_id)
        active_consent(scope, lock=False, acceptance_id=recording.consent_id)
        consent = consent_for_future_use(
            clinic_id=clinic_id,
            enrollment_id=recording.enrollment_id,
            purpose="ai_assistance",
        )
        if consent is None or recording.discarded or scope.encounter.state != "open":
            raise TranscriptAccessDeniedError
    except (ScribeError, CurrentActorError) as error:
        raise TranscriptAccessDeniedError from error
    return chunk, recording, consent


def eligible_input(source: AIArtifact) -> bool:
    """Recheck consent, exact audio identity and assignment at the send boundary."""
    try:
        payload = decode_reference(source.content)
        chunk, recording, consent = _consented(
            clinic_id=source.clinic_id,
            chunk_id=payload.chunk_id,
        )
    except (TranscriptError, TranscriptAccessDeniedError):
        return False
    return (
        source.user_id == recording.physician_id
        and source.patient_enrollment_id == recording.enrollment_id
        and payload.consent_id == consent.pk
        and payload.codec == chunk.codec
        and payload.duration_ms == chunk.duration_ms
    )


def _unseal_audio(*, clinic_id: UUID, chunk_id: UUID, ciphertext: bytes) -> bytes:
    """Recheck authority before decrypting the one object-key-controlled source."""
    chunk, _recording, _consent = _consented(clinic_id=clinic_id, chunk_id=chunk_id)
    try:
        key = reveal(
            purpose=f"scribe.object-key:{chunk.storage_key}",
            envelope=bytes(chunk.wrapped_key),
        ).decode("ascii")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.pgp_sym_decrypt_bytea(%s,%s)",
                [ciphertext, key],
            )
            row = cursor.fetchone()
    except (EnvelopeError, DatabaseError, UnicodeError) as error:
        raise TranscriptAccessDeniedError from error
    if row is None:
        raise TranscriptAccessDeniedError
    audio = bytes(row[0])
    if len(audio) != chunk.size_bytes or sha256(audio).hexdigest() != chunk.sha256:
        raise TranscriptAccessDeniedError
    return audio


def materialize_input(source: AIArtifact) -> str:
    """Raw audio exists only in the ephemeral, repr-hidden provider request."""
    if not eligible_input(source):
        raise TranscriptAccessDeniedError
    payload = decode_reference(source.content)
    audio = _unseal_audio(
        clinic_id=source.clinic_id,
        chunk_id=payload.chunk_id,
        ciphertext=payload.ciphertext,
    )
    return AudioInput(
        payload.chunk_id,
        payload.consent_id,
        payload.codec,
        payload.duration_ms,
        audio,
    ).encode()


def prepare_audio(*, clinic_id: UUID, chunk_id: UUID) -> PreparedAudio:
    """Authorize before revealing the private object key to the worker."""
    chunk, _recording, _consent = _consented(clinic_id=clinic_id, chunk_id=chunk_id)
    return PreparedAudio(
        chunk.pk, chunk.storage_key, media_storage(clinic_id=clinic_id)
    )


@transaction.atomic
def request_transcription(
    *,
    clinic_id: UUID,
    chunk_id: UUID,
    ciphertext: bytes,
) -> TranscriptionStatus:
    """Admit once; policy/configuration/provider failures never discard audio."""
    _chunk, recording, _consent = _consented(
        clinic_id=clinic_id,
        chunk_id=chunk_id,
        lock=True,
    )
    recording = RecordingSession.objects.select_for_update().get(pk=recording.pk)
    chunk, recording, consent = _consented(clinic_id=clinic_id, chunk_id=chunk_id)
    previous = (
        ChunkTranscription.objects.select_related("invocation")
        .filter(chunk=chunk)
        .first()
    )
    if previous is not None:
        return (
            "complete"
            if previous.invocation.status == "succeeded"
            and previous.transcriptversion_set.exists()
            else "queued"
            if previous.invocation.status == "reserved"
            else "transcription_pending"
        )
    if current_snapshot(recording).state == "final":
        return "complete"
    _unseal_audio(clinic_id=clinic_id, chunk_id=chunk.pk, ciphertext=ciphertext)
    source = create_input(
        clinic_id=clinic_id,
        purpose=Purpose.SCRIBE_ASR,
        patient_enrollment_id=recording.enrollment_id,
        method="transcribe",
        content=AudioReference(
            chunk.pk,
            consent.pk,
            chunk.codec,
            chunk.duration_ms,
            ciphertext,
        ).encode(),
    )
    result = invoke(
        clinic_id=clinic_id,
        purpose=Purpose.SCRIBE_ASR,
        principal=recording.physician_id,
        inputs_ref=source.pk,
        budget=Budget(
            Usage(audio_seconds=(chunk.duration_ms + 999) // 1000),
            maximum_micros=1_000_000,
        ),
    )
    if result.invocation_id is None:
        return "transcription_pending"
    request = ChunkTranscription.objects.create(
        organization_id=recording.organization_id,
        session=recording,
        chunk=chunk,
        consent=consent,
        invocation_id=result.invocation_id,
    )
    record_phase1_event(
        "scribe.transcription.queued",
        clinic_id=clinic_id,
        affected_record_id=request.pk,
    )
    return "queued"


def complete_transcription(*, invocation_id: UUID, content: str) -> None:
    """Settle inside the gateway transaction; a late result cannot replace a final."""
    request = (
        ChunkTranscription.objects.select_related(
            "chunk__epoch", "invocation__inputs_ref"
        )
        .filter(invocation_id=invocation_id)
        .first()
    )
    if request is None:
        return
    if not eligible_input(request.invocation.inputs_ref):
        return
    source = decode_reference(request.invocation.inputs_ref.content)
    if (source.chunk_id, source.consent_id) != (request.chunk_id, request.consent_id):
        return
    chunk = request.chunk
    recording = RecordingSession.objects.select_for_update().get(pk=request.session_id)
    previous = refresh_coverage(current_snapshot(recording), recording)
    if previous.state == "final":
        return
    events = parse_batch(content, duration_ms=chunk.duration_ms)
    offset = (
        int((chunk.epoch.started_at - recording.created_at).total_seconds() * 1000)
        + chunk.seq * SLICE_MS
    )
    latency = min(
        max(0, int((timezone.now() - chunk.created_at).total_seconds() * 1000)),
        2**31 - 1,
    )
    for index, event in enumerate(events):
        value = stitch(
            previous if previous.revision else None,
            chunk=ChunkUpdate(
                chunk.pk,
                tuple(
                    replace(
                        item,
                        chunk_id=chunk.pk,
                        segment_id=uuid5(chunk.pk, str(number)),
                        start_ms=offset + item.start_ms,
                        end_ms=offset + item.end_ms,
                    )
                    for number, item in enumerate(event.segments)
                ),
                event.is_final,
            ),
            coverage=previous.coverage,
            recording_closed=previous.recording_closed,
        )
        previous = save_snapshot(
            recording,
            value,
            mutation="recognized",
            recognition=RecognitionReceipt(request, index, latency),
        )
