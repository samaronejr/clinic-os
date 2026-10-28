"""Private, POST-only transcript review with a complete manual-care alternative."""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from itertools import pairwise
from typing import TYPE_CHECKING, cast
from uuid import UUID

from django.db import transaction
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ai.policy import enabled_policy, select_configuration
from apps.comms.adapters import OperationScope
from apps.identity.otp import privileged_totp_required
from apps.scribe.authority import encounter_scope
from apps.scribe.contracts import ScribeError
from apps.scribe.models import AudioChunk, RecordingSession
from apps.scribe.presenter import browser_binding
from apps.scribe.services import finalize_transcript, read_transcript, review_segment
from apps.scribe.tasks import dispatch_transcription
from apps.scribe.transcript_contracts import (
    SegmentReview,
    TranscriptError,
    TranscriptReason,
)
from apps.scribe.transcript_forms import (
    SPEAKER_CHOICES,
    TranscriptCommand,
    TranscriptValues,
)
from apps.scribe.transcription import prepare_audio
from apps.scribe.transcripts import (
    TranscriptAccessDeniedError,
    authorized_recording,
    coverage_for,
)
from apps.scribe.views import continuation

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from apps.scribe.transcript_contracts import Segment


def _runs(segment: Segment) -> list[dict[str, str]]:
    boundaries = sorted(
        {
            0,
            len(segment.text),
            *(span.start for span in segment.uncertainty),
            *(span.end for span in segment.uncertainty),
        }
    )
    result: list[dict[str, str]] = []
    for start, end in pairwise(boundaries):
        kinds = {
            span.kind
            for span in segment.uncertainty
            if span.start < end and span.end > start
        }
        result.append(
            {
                "text": segment.text[start:end],
                "kind": "inaudible"
                if "inaudible" in kinds
                else "uncertain"
                if kinds
                else "",
            }
        )
    return result


def _available(recording: RecordingSession) -> bool:
    try:
        chunk = AudioChunk.objects.filter(session=recording, state="accepted").first()
        if chunk is None:
            return False
        prepare_audio(clinic_id=recording.clinic_id, chunk_id=chunk.pk)
        enabled_policy(Purpose.SCRIBE_ASR)
        select_configuration(clinic_id=recording.clinic_id, purpose=Purpose.SCRIBE_ASR)
    except (GatewayRefusedError, TranscriptAccessDeniedError, ScribeError):
        return False
    return True


def _queue(recording: RecordingSession) -> int:
    count = 0
    for chunk in AudioChunk.objects.filter(
        session=recording,
        state="accepted",
        chunktranscription__isnull=True,
    ):
        if chunk.operation_id is not None:
            transaction.on_commit(
                partial(
                    dispatch_transcription,
                    OperationScope(
                        chunk.operation_id,
                        recording.organization_id,
                        recording.clinic_id,
                        recording.physician_id,
                    ),
                )
            )
            count += 1
    return count


def _apply(values: TranscriptValues, *, clinic_id: UUID, session_id: UUID) -> None:
    revision = values.get("expected_revision")
    if type(revision) is not int:
        raise TranscriptError
    if values["action"] == "finalize":
        finalize_transcript(
            clinic_id=clinic_id,
            session_id=session_id,
            expected_revision=revision,
            acknowledge_incomplete=values.get("acknowledge_incomplete") is True,
        )
        return
    segment = values.get("segment_id")
    speaker = values.get("speaker")
    text = values.get("text")
    if not isinstance(segment, UUID):
        raise TranscriptError
    if values["action"] == "relabel" and isinstance(speaker, str) and speaker:
        review = SegmentReview(segment, speaker=speaker)
    elif values["action"] == "correct" and isinstance(text, str) and text:
        review = SegmentReview(segment, text=text, uncertainty=())
    else:
        raise TranscriptError
    review_segment(
        clinic_id=clinic_id,
        session_id=session_id,
        expected_revision=revision,
        review=review,
    )


def _command(request: HttpRequest) -> TranscriptValues:
    form = TranscriptCommand(request.POST)
    if not form.is_valid():
        raise TranscriptError
    return cast("TranscriptValues", form.cleaned_data)


def _recording(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    session_id: UUID | None,
) -> RecordingSession | None:
    if session_id is not None:
        recording = authorized_recording(clinic_id=clinic_id, session_id=session_id)
        if recording.encounter_id != encounter_id:
            raise TranscriptAccessDeniedError
        return recording
    return (
        RecordingSession.objects.filter(
            clinic_id=clinic_id,
            encounter_id=encounter_id,
            discarded=False,
        )
        .order_by("-created_at")
        .first()
    )


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_POST
def transcript_review(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Resolve the currently selected patient before reading or changing text."""
    context: dict[str, object] = {"clinic_id": clinic_id}
    status = HTTPStatus.OK
    try:
        values = _command(request)
        encounter_id = values["encounter_id"]
        browser_binding(request, clinic_id=clinic_id, encounter_id=encounter_id)
        encounter_scope(clinic_id=clinic_id, encounter_id=encounter_id)
        recording = _recording(
            clinic_id=clinic_id,
            encounter_id=encounter_id,
            session_id=values.get("session_id"),
        )
        context["encounter_id"] = encounter_id
        if recording is not None:
            context["recording"] = recording
            available = _available(recording)
            context["available"] = available
            if values["action"] == "queue" and available:
                context["queued"] = _queue(recording) > 0
            elif values["action"] not in {"open", "refresh", "queue"}:
                try:
                    _apply(values, clinic_id=clinic_id, session_id=recording.pk)
                    context["saved"] = True
                except TranscriptError as error:
                    context["error"] = (
                        _("Transcript changed. Refresh before reviewing this version.")
                        if error.code == TranscriptReason.STALE
                        else _(
                            "Transcript cannot be finalized until recording stops "
                            "and coverage is reviewed."
                        )
                        if error.code
                        in {TranscriptReason.INCOMPLETE, TranscriptReason.OPEN}
                        else _(
                            "Transcript command was not accepted. "
                            "Refresh and try again."
                        )
                    )
                    status = HTTPStatus.CONFLICT
            current = read_transcript(clinic_id=clinic_id, session_id=recording.pk)
            context["transcript"] = current
            context["missing"] = tuple(
                slot
                for slot in coverage_for(recording)
                if current is None
                or slot.chunk_id is None
                or slot.chunk_id not in current.completed_chunks
            )
            context["revision"] = current.revision if current is not None else 0
            context["can_finalize"] = recording.state in {"stopped", "stopped_revoked"}
            context["speaker_choices"] = SPEAKER_CHOICES
            if current is not None:
                context["segments"] = [
                    {"item": value, "runs": _runs(value)} for value in current.segments
                ]
    except (ScribeError, TranscriptAccessDeniedError):
        context = {"clinic_id": clinic_id, "denied": True}
        status = HTTPStatus.FORBIDDEN
    except TranscriptError:
        context = {"clinic_id": clinic_id, "invalid": True}
        status = HTTPStatus.BAD_REQUEST
    return render(request, "scribe/transcript.html", context, status=status)
