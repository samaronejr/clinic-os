"""Teleconsult v2 participant actions: devices, audio-only, reconnect, removal.

Staff authority is todo 6's ``clinic_app.has_permission('clinical.write',
clinic, enrollment)`` for the session patient's enrollment, and the actor must
be the session's bound physician. Patient authority is the validated patient
session bound to the session's patient, clinic and organization. A refusal
raises before any write: no row, event, audit entry, outbox operation or hint.

Participant media state (audio-only, reconnection, removal) is recorded as
immutable session events; the stored session lifecycle is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.comms.models import IntegrationOperation
from apps.consent.services import patient_authority
from apps.core.integration import OperationRequest, enqueue_operation
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.teleconsult.adapters import PARTICIPANT_SUBJECT, PROVIDER
from apps.teleconsult.hints import publish_session_hint
from apps.teleconsult.models import (
    DEVICE_RESULT_VALUES,
    NETWORK_RESULT_VALUES,
    TeleconsultCredential,
    TeleconsultDeviceCheck,
    TeleconsultEvent,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.teleconsult.services import (
    TeleconsultAccessDeniedError,
    TeleconsultConflictError,
    _issue_credential,
    _lock_session,
    derived_state,
    mint_provider_token,
    refresh_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from apps.comms.adapters import OperationScope
    from apps.teleconsult.video_providers import MintedToken

PERMISSION: Final = "clinical.write"
PHYSICIAN: Final = TeleconsultCredential.Role.PHYSICIAN
PATIENT: Final = TeleconsultCredential.Role.PATIENT
_LIVE: Final = (TeleconsultSession.State.WAITING, TeleconsultSession.State.ACTIVE)
_MODE_KINDS: Final = ("audio_only", "video_restored")
_SESSION_CLOSED: Final = "session_closed"
_NOT_IN_ROOM: Final = "not_in_room"
_REVOKE_NAMESPACE: Final = UUID("0b8f7a52-3c1d-4e6f-9a2b-5d4c3e2f1a0b")


@dataclass(frozen=True, slots=True)
class DeviceResults:
    """Closed device-check codes; anything else is refused before a write."""

    camera: str
    microphone: str
    speaker: str
    network: str

    def __post_init__(self) -> None:
        """Reject any value outside the stored vocabularies."""
        if (
            self.camera not in DEVICE_RESULT_VALUES
            or self.microphone not in DEVICE_RESULT_VALUES
            or self.speaker not in DEVICE_RESULT_VALUES
            or self.network not in NETWORK_RESULT_VALUES
        ):
            message = "invalid device result"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Recovery:
    """What a reconnecting participant needs to restore its room state."""

    session: TeleconsultSession
    role: str
    state: str
    audio_only: bool
    peer_audio_only: bool
    peer_present: bool
    credential_expires_at: datetime
    provider_token: MintedToken | None


def _enrollment(session: TeleconsultSession) -> UUID | None:
    return (
        PatientClinicEnrollment.objects.filter(
            clinic_id=session.clinic_id, patient_id=session.patient_id
        )
        .values_list("pk", flat=True)
        .first()
    )


def clinician_session(*, clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
    """Resolve one session for its bound physician holding ``clinical.write``.

    Unknown, foreign-clinic, unpermitted and unassigned actors all get the
    same ``TeleconsultAccessDeniedError`` and nothing is written.
    """
    session = TeleconsultSession.objects.filter(
        pk=session_id, clinic_id=clinic_id
    ).first()
    if session is None:
        raise TeleconsultAccessDeniedError
    try:
        actor = require_permission(
            PERMISSION,
            clinic_id=session.clinic_id,
            patient_enrollment_id=_enrollment(session),
        )
    except CurrentActorError as error:
        raise TeleconsultAccessDeniedError from error
    if actor != session.physician_id:
        raise TeleconsultAccessDeniedError
    return session


def patient_session(*, session_id: UUID) -> TeleconsultSession:
    """Resolve one session bound to the current patient session, else refuse."""
    authority = patient_authority()
    session = TeleconsultSession.objects.filter(pk=session_id).first()
    if session is None or (
        session.patient_id != authority.patient_id
        or session.clinic_id != authority.clinic_id
        or session.organization_id != authority.organization_id
    ):
        raise PatientAccessDeniedError
    return session


def _live(session: TeleconsultSession) -> TeleconsultSession:
    """Persist any discovered failure, then require a waiting/active session."""
    session = refresh_session(session)
    if session.state not in _LIVE:
        raise TeleconsultConflictError(_SESSION_CLOSED)
    return session


def _participant(session: TeleconsultSession, role: str) -> UUID:
    return session.physician_id if role == PHYSICIAN else session.patient_id


def _record_device_check(
    session: TeleconsultSession, role: str, results: DeviceResults
) -> TeleconsultDeviceCheck:
    session = _live(session)
    with transaction.atomic():
        _lock_session(session.pk)
        check = TeleconsultDeviceCheck.objects.create(
            organization_id=session.organization_id,
            session=session,
            role=role,
            participant_id=_participant(session, role),
            camera=results.camera,
            microphone=results.microphone,
            speaker=results.speaker,
            network=results.network,
        )
        publish_session_hint(session)
        return check


def record_device_check(
    *, clinic_id: UUID, session_id: UUID, results: DeviceResults
) -> TeleconsultDeviceCheck:
    """Store the assigned physician's device-check codes for a live session."""
    session = clinician_session(clinic_id=clinic_id, session_id=session_id)
    return _record_device_check(session, PHYSICIAN, results)


def record_patient_device_check(
    *, session_id: UUID, results: DeviceResults
) -> TeleconsultDeviceCheck:
    """Store the bound patient's device-check codes for a live session."""
    session = patient_session(session_id=session_id)
    return _record_device_check(session, PATIENT, results)


def latest_device_check(
    session: TeleconsultSession, role: str
) -> TeleconsultDeviceCheck | None:
    """Return the role's newest device check visible to the current principal."""
    return (
        TeleconsultDeviceCheck.objects.filter(session=session, role=role)
        .order_by("-created_at", "-pk")
        .first()
    )


def audio_only(session: TeleconsultSession, role: str) -> bool:
    """Derive a role's media mode from its newest audio-only/video event."""
    kind = (
        TeleconsultEvent.objects.filter(
            session=session, actor_role=role, kind__in=_MODE_KINDS
        )
        .order_by("-created_at", "-pk")
        .values_list("kind", flat=True)
        .first()
    )
    return kind == "audio_only"


def _entered_credential(
    session: TeleconsultSession, role: str
) -> TeleconsultCredential | None:
    """Return the role's newest credential if it entered the room."""
    credential = (
        TeleconsultCredential.objects.filter(session=session, role=role)
        .order_by("-created_at", "-pk")
        .first()
    )
    if credential is None or credential.first_used_at is None:
        return None
    return credential


def _in_room(session: TeleconsultSession, role: str) -> TeleconsultCredential:
    credential = _entered_credential(session, role)
    if (
        credential is None
        or credential.revoked_at is not None
        or credential.expires_at <= timezone.now()
    ):
        raise TeleconsultConflictError(_NOT_IN_ROOM)
    return credential


def _set_audio_only(session: TeleconsultSession, role: str, *, enabled: bool) -> bool:
    session = _live(session)
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state not in _LIVE:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        _in_room(session, role)
        if audio_only(session, role) != enabled:
            TeleconsultEvent.objects.create(
                organization_id=session.organization_id,
                session=session,
                kind="audio_only" if enabled else "video_restored",
                actor_role=role,
            )
            publish_session_hint(session)
    return enabled


def set_audio_only(*, clinic_id: UUID, session_id: UUID, enabled: bool) -> bool:
    """Switch the assigned physician between audio-only and video."""
    session = clinician_session(clinic_id=clinic_id, session_id=session_id)
    return _set_audio_only(session, PHYSICIAN, enabled=enabled)


def set_patient_audio_only(*, session_id: UUID, enabled: bool) -> bool:
    """Switch the bound patient between audio-only and video."""
    session = patient_session(session_id=session_id)
    return _set_audio_only(session, PATIENT, enabled=enabled)


def _resume(session: TeleconsultSession, role: str) -> Recovery:
    """Restore a participant after a drop; an expired credential is rotated.

    Only a participant that entered and was never revoked (ended, removed or
    rotated away) may resume; authority comes from the current login or
    patient session, never from the stored token.
    """
    session = _live(session)
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state not in _LIVE:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        credential = _entered_credential(session, role)
        if credential is None or credential.revoked_at is not None:
            raise TeleconsultConflictError(_NOT_IN_ROOM)
        if credential.expires_at <= timezone.now():
            credential = _issue_credential(
                session, role, _participant(session, role)
            ).credential
            credential.first_used_at = timezone.now()
            credential.save(update_fields=("first_used_at",))
        TeleconsultEvent.objects.create(
            organization_id=session.organization_id,
            session=session,
            kind="reconnected",
            actor_role=role,
        )
        room = TeleconsultRoom.objects.get(session=session)
        token = mint_provider_token(room, credential)
        publish_session_hint(session)
        peer = PATIENT if role == PHYSICIAN else PHYSICIAN
        peer_credential = _entered_credential(session, peer)
        return Recovery(
            session=session,
            role=role,
            state=derived_state(session),
            audio_only=audio_only(session, role),
            peer_audio_only=audio_only(session, peer),
            peer_present=peer_credential is not None
            and peer_credential.revoked_at is None,
            credential_expires_at=credential.expires_at,
            provider_token=token,
        )


def resume_physician(*, clinic_id: UUID, session_id: UUID) -> Recovery:
    """Reconnect the assigned physician with state recovery."""
    session = clinician_session(clinic_id=clinic_id, session_id=session_id)
    return _resume(session, PHYSICIAN)


def resume_patient(*, session_id: UUID) -> Recovery:
    """Reconnect the bound patient with state recovery."""
    session = patient_session(session_id=session_id)
    return _resume(session, PATIENT)


def remove_patient(*, clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
    """Disconnect the patient: revoke the credential, then revoke at the provider.

    The provider revoke is a committed outbox operation run after commit. The
    patient can re-enter only through a fresh join from the waiting room.
    """
    session = _live(clinician_session(clinic_id=clinic_id, session_id=session_id))
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state not in _LIVE:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        credential = _in_room(session, PATIENT)
        credential.revoked_at = timezone.now()
        credential.save(update_fields=("revoked_at",))
        TeleconsultEvent.objects.create(
            organization_id=session.organization_id,
            session=session,
            kind="removed",
            actor_role=PHYSICIAN,
        )
        room = TeleconsultRoom.objects.get(session=session)
        enqueue_operation(
            OperationRequest(
                channel=IntegrationOperation.Channel.VIDEO,
                provider=room.provider or PROVIDER,
                clinic_id=clinic_id,
                subject_type=PARTICIPANT_SUBJECT,
                subject_id=credential.pk,
                idempotency_key=uuid5(_REVOKE_NAMESPACE, str(credential.pk)),
                max_attempts=3,
            )
        )
        record_phase1_event(
            "teleconsult.participant.removed",
            clinic_id=clinic_id,
            affected_record_id=session.pk,
        )
        publish_session_hint(session)
        return session


def participant_revoke_eligible(scope: OperationScope) -> bool:
    """Recheck the stored revoked credential immediately before provider revoke.

    Registered for ``PARTICIPANT_SUBJECT``: the operation must name a
    credential of this tenant and clinic that is already revoked.
    """
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    credential = (
        TeleconsultCredential.objects.filter(
            pk=operation.subject_id, revoked_at__isnull=False
        )
        .select_related("session")
        .first()
    )
    return (
        credential is not None
        and credential.session.organization_id == scope.organization_id
        and credential.session.clinic_id == scope.clinic_id
        and operation.subject_type == PARTICIPANT_SUBJECT
    )


def patient_removed(session: TeleconsultSession) -> bool:
    """Whether the newest patient credential was revoked by a removal."""
    credential = _entered_credential(session, PATIENT)
    if credential is None or credential.revoked_at is None:
        return False
    return TeleconsultEvent.objects.filter(
        session=session, kind="removed", created_at__gte=credential.first_used_at
    ).exists()


def authorize_room_topic(*, room_name: str) -> UUID:
    """Authorize the physician's realtime topic for one opaque room.

    Returns the clinic for the subscription audit; any other principal gets
    ``TeleconsultAccessDeniedError``. Rechecked on every stream frame.
    """
    session_id = (
        TeleconsultRoom.objects.filter(room_name=room_name)
        .values_list("session_id", flat=True)
        .first()
    )
    session = (
        TeleconsultSession.objects.filter(pk=session_id).first()
        if session_id is not None
        else None
    )
    if session is None:
        raise TeleconsultAccessDeniedError
    return clinician_session(
        clinic_id=session.clinic_id, session_id=session.pk
    ).clinic_id


__all__ = (
    "DeviceResults",
    "Recovery",
    "audio_only",
    "authorize_room_topic",
    "clinician_session",
    "latest_device_check",
    "participant_revoke_eligible",
    "patient_removed",
    "patient_session",
    "record_device_check",
    "record_patient_device_check",
    "remove_patient",
    "resume_patient",
    "resume_physician",
    "set_audio_only",
    "set_patient_audio_only",
)
