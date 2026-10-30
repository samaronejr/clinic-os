"""The shell may stop/discard bound capture, never retarget it to another patient."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.utils.translation import gettext

from apps.core.patient_context import SwitchConsequence
from apps.identity.current_context import current_actor_id
from apps.scribe.authority import issue_encounter_token, lock_recording_consent
from apps.scribe.contracts import LIVE_STATES
from apps.scribe.controls import control_recording
from apps.scribe.models import AudioChunk, RecordingSession

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from apps.core.patient_context import ContextSwitch


def bound_recordings(switch: ContextSwitch) -> QuerySet[RecordingSession]:
    """Only the current physician's old patient can block a context change."""
    return RecordingSession.objects.filter(
        clinic_id=switch.clinic_id,
        enrollment_id=switch.from_enrollment_id,
        physician_id=current_actor_id(),
        state__in=LIVE_STATES,
    )


class CaptureSwitchGuard:
    """Keep the current patient or explicitly stop and crypto-discard capture."""

    def check(self, switch: ContextSwitch) -> SwitchConsequence | None:
        """Require an explicit choice before switching live bound work."""
        if switch.from_enrollment_id == switch.to_enrollment_id:
            return None
        lock_recording_consent(clinic_id=switch.clinic_id)
        recordings = tuple(bound_recordings(switch).select_for_update().order_by("pk"))
        if not recordings:
            return None
        tuple(
            AudioChunk.objects.select_for_update()
            .filter(session__in=recordings, state__in=("buffered", "accepted"))
            .order_by("pk")
        )
        return SwitchConsequence(
            "scribe.capture",
            gettext(
                "Audio capture belongs to this patient. Keep this patient, "
                "or stop and discard capture before switching."
            ),
        )

    def discard(self, switch: ContextSwitch) -> None:
        """Use a new server token to stop, never change the recording's binding."""
        for recording in bound_recordings(switch):
            token = issue_encounter_token(
                clinic_id=switch.clinic_id,
                encounter_id=recording.encounter_id,
                browser_binding="server-context-switch",
            )
            # An assigned clinician can stop with a fresh encounter token even
            # after consent withdrawal; that token can never resume old capture.
            control_recording(
                clinic_id=switch.clinic_id,
                session_id=recording.pk,
                encounter_token=token,
                command_id=uuid4(),
                action="cancel",
                expected_epoch=recording.epoch,
                reason="patient_switch",
            )
