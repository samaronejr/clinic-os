"""Bounded POST-only recording controls and chunk ingest with normal CSRF."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict
from hashlib import sha256
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID

from django.core import signing
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from rest_framework import serializers

from apps.identity.otp import privileged_totp_required
from apps.scribe.authority import (
    TOKEN_MAX_AGE,
    TOKEN_SALT,
    active_consent,
    recording_scope,
)
from apps.scribe.contracts import (
    BUFFER_MS,
    CODECS,
    MAX_CHUNK_BYTES,
    MAX_DURATION_MS,
    MAX_SEQUENCE,
    SLICE_MS,
    TRACKS,
    ChunkInput,
    Reason,
    ScribeError,
)
from apps.scribe.models import CaptureEpoch
from apps.scribe.presenter import browser_binding
from apps.scribe.services import control_recording, create_recording, ingest_chunk

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

CONTROL_ACTIONS = (
    "authorize",
    "start",
    "pause",
    "resume",
    "stop",
    "cancel",
    "recover",
    "status",
)
MAX_BODY_BYTES = 360 * 1024


class ControlInput(serializers.Serializer[dict[str, object]]):
    """Bound every native and JSON command field before the service sees it."""

    action = serializers.ChoiceField(choices=CONTROL_ACTIONS)
    encounter_token = serializers.CharField(max_length=2048)
    command_id = serializers.UUIDField()
    session_id = serializers.UUIDField(required=False)
    expected_epoch = serializers.UUIDField(
        required=False, allow_null=True, default=None
    )
    last_seq = serializers.IntegerField(
        min_value=-1, max_value=MAX_SEQUENCE, default=-1
    )
    reason = serializers.ChoiceField(
        choices=("user", "offline", "microphone", "patient_switch", "recovery"),
        default="user",
    )


class ChunkUpload(serializers.Serializer[dict[str, object]]):
    """Keep the public chunk-body contract, with its token in a request header."""

    session_id = serializers.UUIDField()
    epoch = serializers.UUIDField()
    track = serializers.ChoiceField(choices=TRACKS)
    seq = serializers.IntegerField(min_value=0, max_value=MAX_SEQUENCE)
    sha256 = serializers.RegexField(regex=r"^[a-f0-9]{64}$")
    duration_ms = serializers.IntegerField(min_value=1, max_value=MAX_DURATION_MS)
    codec = serializers.ChoiceField(choices=CODECS)
    bytes = serializers.CharField(max_length=4 * ((MAX_CHUNK_BYTES + 2) // 3))


def _input(request: HttpRequest) -> object:
    length = request.META.get("CONTENT_LENGTH", "")
    if (
        not isinstance(length, str)
        or not length.isdecimal()
        or not 1 <= int(length) <= MAX_BODY_BYTES
    ):
        raise ScribeError(Reason.INVALID_COMMAND)
    if request.content_type != "application/json":
        return request.POST
    try:
        return json.loads(request.body)
    except (ValueError, UnicodeError) as error:
        raise ScribeError(Reason.INVALID_COMMAND) from error


def _binding(request: HttpRequest, clinic_id: UUID, token: str) -> UUID:
    """Require the selected encounter and token's browser to agree."""
    try:
        terms = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        if not isinstance(terms, dict):
            raise ScribeError
        encounter = UUID(terms["encounter"])
        browser = browser_binding(request, clinic_id=clinic_id, encounter_id=encounter)
        if terms.get("browser") != sha256(browser.encode()).hexdigest():
            raise ScribeError
    except (signing.BadSignature, ValueError, KeyError, TypeError) as error:
        raise ScribeError from error
    return encounter


def continuation(clinic_id: UUID) -> str:
    """Resume authentication without an encounter selector or capture token."""
    return reverse("ehr:encounter", args=(clinic_id,))


def _epoch_token(session_id: UUID, number: int) -> str | None:
    if number == 0:
        return None
    epoch = CaptureEpoch.objects.filter(session_id=session_id, number=number).first()
    if epoch is None:
        raise ScribeError(Reason.EPOCH_STALE)
    return str(epoch.pk)


def _epoch_number(session_id: UUID, token: UUID | None) -> int:
    if token is None:
        return 0
    epoch = CaptureEpoch.objects.filter(session_id=session_id, pk=token).first()
    if epoch is None:
        raise ScribeError(Reason.EPOCH_STALE)
    return epoch.number


def _refusal(error: ScribeError) -> JsonResponse:
    status = (
        HTTPStatus.FORBIDDEN
        if error.code in {Reason.ACCESS_DENIED, Reason.CONSENT_REVOKED}
        else HTTPStatus.BAD_REQUEST
        if error.code in {Reason.INVALID_COMMAND, Reason.INVALID_CHUNK}
        else HTTPStatus.CONFLICT
    )
    return JsonResponse({"ack": "rejected", "code": error.code}, status=status)


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_POST
def capture_control(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Native controls retain a complete manual path; JS alone opens a mic."""
    try:
        parsed = ControlInput(data=_input(request))
        if not parsed.is_valid():
            return _refusal(ScribeError(Reason.INVALID_COMMAND))
        values = parsed.validated_data
        token = values["encounter_token"]
        encounter_id = _binding(request, clinic_id, token)
        action = values["action"]
        if action == "authorize":
            recording = create_recording(
                clinic_id=clinic_id,
                encounter_token=token,
                command_id=values["command_id"],
            )
            result = {
                "session_id": str(recording.pk),
                "state": recording.state,
                "epoch": _epoch_token(recording.pk, recording.epoch),
            }
        else:
            session_id = values.get("session_id")
            if session_id is None:
                return _refusal(ScribeError(Reason.INVALID_COMMAND))
            if action == "status":
                recording, scope = recording_scope(
                    clinic_id=clinic_id,
                    session_id=session_id,
                    consent_required=False,
                )
                if (
                    recording.encounter_id != encounter_id
                    or recording.token_digest != sha256(token.encode()).hexdigest()
                ):
                    return _refusal(ScribeError())
                code = (
                    ""
                    if scope.encounter.state == "open"
                    else Reason.SESSION_NOT_RECORDING
                )
                try:
                    active_consent(
                        scope, lock=False, acceptance_id=recording.consent_id
                    )
                except ScribeError as error:
                    code = error.code
                result = {
                    "session_id": str(recording.pk),
                    "state": recording.state,
                    "epoch": _epoch_token(recording.pk, recording.epoch),
                    "code": code,
                }
            else:
                receipt = control_recording(
                    clinic_id=clinic_id,
                    session_id=session_id,
                    encounter_token=token,
                    command_id=values["command_id"],
                    action=action,
                    expected_epoch=_epoch_number(session_id, values["expected_epoch"]),
                    last_seq=values["last_seq"],
                    reason=values["reason"],
                )
                result = {
                    "session_id": str(receipt.session_id),
                    "state": receipt.state,
                    "epoch": _epoch_token(receipt.session_id, receipt.epoch),
                }
    except ScribeError as error:
        return _refusal(error)
    if request.content_type != "application/json":
        return redirect(reverse("ehr:encounter", args=(clinic_id,)))
    return JsonResponse({**result, "slice_ms": SLICE_MS, "buffer_ms": BUFFER_MS})


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_POST
def capture_chunk(request: HttpRequest, clinic_id: UUID) -> JsonResponse:
    """Verify bytes before any chunk, key, gap or outbox mutation."""
    try:
        token = request.headers.get("X-Encounter-Token", "")
        _binding(request, clinic_id, token)
        parsed = ChunkUpload(data=_input(request))
        if not parsed.is_valid():
            return _refusal(ScribeError(Reason.INVALID_CHUNK))
        values = parsed.validated_data
        try:
            data = base64.b64decode(values["bytes"], validate=True)
        except (binascii.Error, ValueError) as error:
            raise ScribeError(Reason.INVALID_CHUNK) from error
        result = ingest_chunk(
            clinic_id=clinic_id,
            encounter_token=token,
            chunk=ChunkInput(
                session_id=values["session_id"],
                epoch=values["epoch"],
                track=values["track"],
                seq=values["seq"],
                sha256=values["sha256"],
                duration_ms=values["duration_ms"],
                codec=values["codec"],
                data=data,
            ),
        )
    except ScribeError as error:
        return _refusal(error)
    if result.ack == "rejected":
        return _refusal(ScribeError(Reason(result.code)))
    return JsonResponse(asdict(result))
