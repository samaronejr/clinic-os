"""Serialized, idempotent recording commands; recovery fences the abandoned tab."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import rfc8785
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.providers.services import current_version
from apps.scribe.authority import (
    EncounterScope,
    active_consent,
    lock_recording_consent,
    recording_scope,
    token_scope,
)
from apps.scribe.contracts import (
    LIVE_STATES,
    MAX_SEQUENCE,
    ControlAction,
    ControlResult,
    GapReason,
    Reason,
    ScribeError,
)
from apps.scribe.models import (
    AudioChunk,
    CaptureEpoch,
    RecordingCommand,
    RecordingSession,
)
from apps.scribe.storage import media_storage

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from django.db.models import QuerySet


def utc_now() -> datetime:
    """Clock seam for deterministic expiry and interruption tests."""
    return timezone.now()


def create_recording(
    *, clinic_id: UUID, encounter_token: str, command_id: UUID
) -> RecordingSession:
    """Create and authorize once; no microphone has been opened by this command."""
    with transaction.atomic():
        scope = token_scope(clinic_id=clinic_id, token=encounter_token)
        consent = active_consent(scope)
        media_storage(clinic_id=clinic_id)
        version = current_version("object_storage_media", clinic_id=clinic_id)
        if version is None:
            raise ScribeError(Reason.STORAGE_UNAVAILABLE)
        # Converge parallel starts before allocating the one-live-recording key.
        locked = (
            type(scope.encounter).objects.select_for_update().get(pk=scope.encounter.pk)
        )
        if locked.state != "open":
            raise ScribeError(Reason.SESSION_NOT_RECORDING)
        digest = sha256(encounter_token.encode()).hexdigest()
        existing = RecordingSession.objects.filter(
            clinic_id=clinic_id,
            physician_id=scope.physician_id,
            creation_key=command_id,
        ).first()
        if existing is not None:
            if (
                existing.encounter_id != scope.encounter.pk
                or existing.token_digest != digest
                or existing.clinic_id != clinic_id
            ):
                raise ScribeError(Reason.IDEMPOTENCY_MISMATCH)
            return existing
        if RecordingSession.objects.filter(
            encounter=scope.encounter, state__in=LIVE_STATES
        ).exists():
            raise ScribeError(Reason.ALREADY_ACTIVE)
        recording = RecordingSession.objects.create(
            creation_key=command_id,
            organization_id=scope.encounter.organization_id,
            clinic_id=clinic_id,
            encounter=scope.encounter,
            enrollment=scope.enrollment,
            physician_id=scope.physician_id,
            consent=consent,
            capability_version_id=version.pk,
            capability_snapshot={
                "capability": "object_storage_media",
                "provider": version.provider,
                "account": version.account,
                "environment": version.environment,
                "api_version": version.api_version,
                "region": version.region,
                "retention_terms": version.retention_terms,
            },
            token_digest=digest,
        )
        record_phase1_event(
            "scribe.session.created",
            clinic_id=clinic_id,
            affected_record_id=recording.pk,
        )
        recording.state = "authorized"
        recording.revision += 1
        recording.save(update_fields=("state", "revision"))
        record_phase1_event(
            "scribe.session.authorized",
            clinic_id=clinic_id,
            affected_record_id=recording.pk,
        )
        return recording


def gap_ranges(sequences: set[int], high: int) -> list[list[int]]:
    """Represent absent slices as inclusive ranges, never synthesized content."""
    gaps: list[list[int]] = []
    for seq in range(high + 1):
        if seq in sequences:
            continue
        if gaps and gaps[-1][1] == seq - 1:
            gaps[-1][1] = seq
        else:
            gaps.append([seq, seq])
    return gaps


def update_gaps(epoch: CaptureEpoch, *, track: str, last_seq: int) -> None:
    """Keep declared but absent slices after expiry, cancellation or interruption."""
    high = max(epoch.high_water.get(track, -1), last_seq)
    epoch.high_water = {**epoch.high_water, track: high}
    present = set(
        epoch.chunks.filter(
            track=track, state__in=("buffered", "accepted")
        ).values_list("seq", flat=True)
    )
    gaps = gap_ranges(present, high)
    epoch.gaps = {**epoch.gaps, track: gaps}
    epoch.save(update_fields=("high_water", "gaps"))


def _close_epoch(recording: RecordingSession, last_seq: int, reason: str) -> None:
    epoch = CaptureEpoch.objects.filter(
        session=recording, number=recording.epoch, ended_at__isnull=True
    ).first()
    if epoch is None:
        return
    if last_seq >= 0:
        update_gaps(epoch, track="microphone", last_seq=last_seq)
    epoch.ended_at = utc_now()
    epoch.reason = reason
    epoch.save(update_fields=("ended_at", "reason"))
    record_phase1_event(
        "scribe.epoch.closed",
        clinic_id=recording.clinic_id,
        affected_record_id=epoch.pk,
    )


def _permit_start(recording: RecordingSession, scope: EncounterScope) -> None:
    """Decide consent and provider refusals before any write."""
    if scope.encounter.state != "open":
        raise ScribeError(Reason.SESSION_NOT_RECORDING)
    active_consent(scope, acceptance_id=recording.consent_id)
    version = current_version("object_storage_media", clinic_id=recording.clinic_id)
    media_storage(clinic_id=recording.clinic_id)
    if version is None or version.pk != recording.capability_version_id:
        raise ScribeError(Reason.STORAGE_UNAVAILABLE)


def _discard_audio(recording: RecordingSession) -> None:
    """Destroy the sole wrapped per-object keys, retaining ciphertext history."""
    chunks: QuerySet[AudioChunk] = AudioChunk.objects.filter(
        session=recording, state__in=("buffered", "accepted")
    )
    for item in chunks:
        item.state = "rejected"
        item.wrapped_key = b""
        item.encrypted_data = b""
        item.save(update_fields=("state", "wrapped_key", "encrypted_data"))
        record_phase1_event(
            "scribe.chunk.rejected",
            clinic_id=recording.clinic_id,
            affected_record_id=item.pk,
        )


def _transition(
    recording: RecordingSession,
    action: ControlAction,
    scope: EncounterScope,
    *,
    last_seq: int,
    reason: GapReason,
) -> None:
    """Change only the fields owned by this legal state transition."""
    if recording.state not in LIVE_STATES:
        return
    if action == "cancel":
        # Lock every affected worker row before the first audit append.
        tuple(
            AudioChunk.objects.select_for_update()
            .filter(session=recording, state__in=("buffered", "accepted"))
            .order_by("pk")
        )
    _close_epoch(recording, last_seq, reason)
    if action in {"start", "resume"}:
        recording.epoch += 1
        recording.state = "recording"
    elif action in {"pause", "recover"}:
        recording.state = "paused"
    else:
        try:
            active_consent(scope, acceptance_id=recording.consent_id)
            target = "stopped"
        except ScribeError:
            target = "stopped_revoked"
        recording.state = target
        recording.stopped_at = recording.stopped_at or utc_now()
        recording.discarded = action == "cancel" or recording.discarded
    recording.revision += 1
    recording.save(
        update_fields=(
            "state",
            "epoch",
            "revision",
            "discarded",
            "stopped_at",
            "token_digest",
        )
    )
    record_phase1_event(
        f"scribe.session.{recording.state}",
        clinic_id=recording.clinic_id,
        affected_record_id=recording.pk,
    )
    if action in {"start", "resume"}:
        epoch = CaptureEpoch.objects.create(
            organization_id=recording.organization_id,
            session=recording,
            number=recording.epoch,
        )
        record_phase1_event(
            "scribe.epoch.created",
            clinic_id=recording.clinic_id,
            affected_record_id=epoch.pk,
        )
    if recording.discarded:
        _discard_audio(recording)


def _replayed_control(
    recording: RecordingSession,
    command_id: UUID,
    payload_digest: str,
    *,
    start: bool,
) -> ControlResult | None:
    """Return the original outcome only while its current authority still holds."""
    receipt = RecordingCommand.objects.filter(
        session=recording, command_id=command_id
    ).first()
    if receipt is None:
        return None
    if receipt.payload_digest != payload_digest:
        raise ScribeError(Reason.IDEMPOTENCY_MISMATCH)
    if start and recording.state != "recording":
        raise ScribeError(Reason.SESSION_NOT_RECORDING)
    if start and receipt.result_epoch != recording.epoch:
        raise ScribeError(Reason.EPOCH_STALE)
    return ControlResult(recording.pk, receipt.result_state, receipt.result_epoch)


def control_recording(  # noqa: PLR0913 - exact bounded command contract
    *,
    clinic_id: UUID,
    session_id: UUID,
    encounter_token: str,
    command_id: UUID,
    action: ControlAction,
    expected_epoch: int,
    last_seq: int = -1,
    reason: GapReason = "user",
) -> ControlResult:
    """Decide every refusal before the first state change or command receipt."""
    if (
        action not in {"start", "pause", "resume", "stop", "cancel", "recover"}
        or type(expected_epoch) is not int
        or type(last_seq) is not int
        or not -1 <= last_seq <= MAX_SEQUENCE
        or reason not in {"user", "offline", "microphone", "patient_switch", "recovery"}
    ):
        raise ScribeError(Reason.INVALID_COMMAND)
    with transaction.atomic():
        scope = token_scope(clinic_id=clinic_id, token=encounter_token)
        lock_recording_consent(clinic_id=clinic_id)
        recording, _scope = recording_scope(
            clinic_id=clinic_id, session_id=session_id, consent_required=False
        )
        if recording.encounter_id != scope.encounter.pk:
            raise ScribeError
        recording = RecordingSession.objects.select_for_update().get(pk=recording.pk)
        digest = sha256(encounter_token.encode()).hexdigest()
        if (
            action not in {"recover", "cancel", "stop"}
            and recording.token_digest != digest
        ):
            raise ScribeError
        start = action in {"start", "resume"}
        if start:
            _permit_start(recording, scope)
        if action == "recover":
            active_consent(scope, acceptance_id=recording.consent_id)
        payload_digest = sha256(
            rfc8785.dumps(
                {
                    "session": str(session_id),
                    "token_digest": digest,
                    "action": action,
                    "epoch": expected_epoch,
                    "last_seq": last_seq,
                    "reason": reason,
                }
            )
        ).hexdigest()
        replayed = _replayed_control(recording, command_id, payload_digest, start=start)
        if replayed is not None:
            return replayed
        if recording.epoch != expected_epoch:
            raise ScribeError(Reason.EPOCH_STALE)
        allowed = {
            "start": {"authorized"},
            "resume": {"paused"},
            "pause": {"recording"},
            "recover": {"authorized", "recording", "paused"},
            "stop": set(LIVE_STATES) | {"stopped", "stopped_revoked"},
            "cancel": set(LIVE_STATES),
        }
        if recording.state not in allowed[action]:
            raise ScribeError(Reason.SESSION_NOT_RECORDING)
        recording.token_digest = (
            digest if action == "recover" else recording.token_digest
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.scribe_recovery',%s,true)",
                [str(action == "recover").lower()],
            )
        # A terminal stop replay under a new command key is read-only apart
        # from its own receipt; terminal rows never receive another revision.
        _transition(recording, action, scope, last_seq=last_seq, reason=reason)
        RecordingCommand.objects.create(
            command_id=command_id,
            organization_id=recording.organization_id,
            session=recording,
            action=action,
            payload_digest=payload_digest,
            result_state=recording.state,
            result_epoch=recording.epoch,
        )
        return ControlResult(recording.pk, recording.state, recording.epoch)
