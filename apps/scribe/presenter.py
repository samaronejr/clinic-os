"""Read-only encounter capture presentation; a refusal never mutates a session."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.core.patient_context import SESSION_PREFIX, patient_context_allowed
from apps.scribe.authority import active_consent, encounter_scope, issue_encounter_token
from apps.scribe.contracts import LIVE_STATES, ScribeError
from apps.scribe.models import CaptureEpoch, RecordingSession
from apps.scribe.storage import media_storage

if TYPE_CHECKING:
    from django.http import HttpRequest


@dataclass(frozen=True, slots=True)
class CapturePanel:
    """Only control metadata reaches the template; no audio reaches the DOM."""

    available: bool
    token: str = field(default="", repr=False)
    command_id: UUID | None = None
    recording: RecordingSession | None = None
    epoch_token: str = ""


def capture_panel(
    request: HttpRequest, *, clinic_id: UUID, encounter_id: UUID
) -> CapturePanel:
    """Hide capture on refused/missing consent while leaving manual care intact."""
    try:
        scope = encounter_scope(clinic_id=clinic_id, encounter_id=encounter_id)
    except ScribeError:
        return CapturePanel(available=False)
    key = request.session.session_key
    if not key:
        return CapturePanel(available=False)
    recording = RecordingSession.objects.filter(
        encounter_id=encounter_id, state__in=LIVE_STATES
    ).first()
    available = scope.encounter.state == "open"
    try:
        active_consent(
            scope,
            lock=False,
            acceptance_id=recording.consent_id if recording else None,
        )
        media_storage(clinic_id=clinic_id)
    except ScribeError:
        available = False
    return CapturePanel(
        available,
        issue_encounter_token(
            clinic_id=clinic_id, encounter_id=encounter_id, browser_binding=key
        ),
        uuid4(),
        recording,
        str(CaptureEpoch.objects.get(session=recording, number=recording.epoch).pk)
        if recording is not None and recording.epoch
        else "",
    )


def browser_binding(
    request: HttpRequest, *, clinic_id: UUID, encounter_id: UUID
) -> str:
    """Prevent another tab's patient selection from redirecting an upload."""
    selected = request.session.get(f"ehr.encounter.{clinic_id}")
    scope = encounter_scope(clinic_id=clinic_id, encounter_id=encounter_id)
    if (
        selected != str(encounter_id)
        or request.session.get(f"{SESSION_PREFIX}{clinic_id}")
        != str(scope.enrollment.pk)
        or not patient_context_allowed(
            clinic_id=clinic_id, enrollment_id=scope.enrollment.pk
        )
        or not request.session.session_key
    ):
        raise ScribeError
    return request.session.session_key
