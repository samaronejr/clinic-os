"""Scoped teleconsult sessions: stored authority, outbox rooms, join credentials.

Every predicate derives from stored rows inside the current tenant or patient
transaction: the encounter binds appointment, patient, clinic and assigned
physician; the consent acceptance binds the exact published text version;
provider rooms exist only as committed outbox operations; join credentials
are short-lived, role-scoped digests that never reopen ended sessions.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID, uuid5

from django.db import connection, transaction
from django.utils import timezone

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event, record_phase1_event
from apps.comms.models import IntegrationOperation
from apps.consent.models import ConsentAcceptance, ConsentText
from apps.consent.services import consent_for_future_use, patient_authority
from apps.core.integration import OperationRequest, enqueue_operation
from apps.ehr.models import Encounter
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.teleconsult.adapters import PROVIDER
from apps.teleconsult.models import (
    TeleconsultCredential,
    TeleconsultEvent,
    TeleconsultRoom,
    TeleconsultSession,
)

if TYPE_CHECKING:
    from apps.comms.adapters import OperationScope

SUBJECT_TYPE: str = "teleconsult.session"
CREDENTIAL_TTL: timedelta = timedelta(minutes=15)
TOKEN_BYTES: int = 32
_IDEMPOTENCY_NAMESPACE: UUID = UUID("6f1c9d2e-7a4b-4c5d-9e8f-0a1b2c3d4e5f")
_LIVE_STATES = (
    TeleconsultSession.State.WAITING,
    TeleconsultSession.State.ACTIVE,
)
_TERMINAL_STATES = (
    TeleconsultSession.State.ENDED,
    TeleconsultSession.State.FAILED,
)
_SESSION_CLOSED = "session_closed"
_ROOM_NOT_READY = "room_not_ready"
_ENCOUNTER_CLOSED = "encounter_closed"
_CONSENT_REQUIRED = "consent_required"


class TeleconsultAccessDeniedError(Exception):
    """Non-enumerating teleconsult boundary failure."""


class TeleconsultConflictError(Exception):
    """A fixed lifecycle conflict; no caller content appears in its message."""

    def __init__(self, reason_code: str) -> None:
        """Retain the machine-consumed conflict reason."""
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """The raw join token shown exactly once; only its digest is stored."""

    credential: TeleconsultCredential
    token: str


@dataclass(frozen=True, slots=True)
class RoomEntry:
    """The resolved room facts for one admitted participant."""

    session: TeleconsultSession
    room_name: str
    role: str
    credential: TeleconsultCredential


@dataclass(frozen=True, slots=True)
class _SessionScope:
    """Stored session facts resolved through the SECURITY DEFINER catalog."""

    clinic_id: UUID
    patient_id: UUID
    physician_id: UUID
    state: str
    encounter_state: str
    consent_id: UUID


def _tenant_guc_present() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT NULLIF(current_setting('app.current_tenant', true), '')")
        row = cursor.fetchone()
    return row is not None and row[0] is not None


def _denied(session_id: UUID | None, clinic_id: UUID | None, reason: str) -> NoReturn:
    """Append the fixed metadata-only denial event, then fail closed.

    Patient requests carry no tenant GUCs, so the staff-chain audit append is
    skipped there; the scoped ``join_denied`` history row still records it.
    """
    if session_id is not None and clinic_id is not None and _tenant_guc_present():
        record_event(
            AuditEventInput(
                event_type="teleconsult.access.denied",
                component_id="clinic-os-web",
                component_ip=None,
                affected_record_type="teleconsult.session",
                affected_record_id=str(session_id),
                occurred_at_utc=timezone.now(),
            ),
            payload={"clinic_id": str(clinic_id), "reason_code": reason},
        )
    raise TeleconsultAccessDeniedError


def _event(
    session: TeleconsultSession,
    kind: str,
    *,
    actor_role: str = "",
    reason_code: str = "",
) -> TeleconsultEvent:
    """Append one immutable scoped-history row for the session."""
    return TeleconsultEvent.objects.create(
        organization_id=session.organization_id,
        session=session,
        kind=kind,
        actor_role=actor_role,
        reason_code=reason_code,
    )


def _lock_session(key: UUID) -> None:
    """Serialize session transitions on one transaction-scoped advisory lock."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"teleconsult.session:{key}"],
        )


def _session_scope(session_id: UUID) -> _SessionScope | None:
    """Resolve stored session facts in any principal context."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.teleconsult_session_scope(%s)",
            [str(session_id)],
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return _SessionScope(*row)


def _room_state(session_id: UUID) -> str:
    """Return the stored room operation status, or ``none`` without a room."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.teleconsult_room_state(%s)", [str(session_id)]
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return "none"
    return str(row[0])


def _room_ready(session_id: UUID) -> bool:
    return _room_state(session_id) in (
        IntegrationOperation.Status.SUCCEEDED,
        IntegrationOperation.Status.DELIVERED,
    )


def _assigned_physician(session: TeleconsultSession) -> UUID:
    """Require the current actor to be the session's bound physician."""
    try:
        actor = require_current_actor_clinic_roles(
            session.clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError:
        _denied(session.pk, session.clinic_id, "role_denied")
    if actor != session.physician_id:
        _denied(session.pk, session.clinic_id, "not_assigned")
    return actor


def _staff_session(clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
    """Resolve one session for a staff actor without enumerating other clinics."""
    session = TeleconsultSession.objects.filter(
        pk=session_id, clinic_id=clinic_id
    ).first()
    if session is None:
        raise TeleconsultAccessDeniedError
    try:
        require_current_actor_clinic_roles(clinic_id, tuple(UserClinicRole.Role))
    except CurrentActorError:
        _denied(session.pk, clinic_id, "role_denied")
    return session


def _consent_is_active(session: TeleconsultSession) -> bool:
    """Recheck the bound consent at the point of use; never cache the decision."""
    acceptance = (
        ConsentAcceptance.objects.filter(pk=session.consent_id, revocation__isnull=True)
        .select_related("text")
        .first()
    )
    if acceptance is None:
        return False
    current = (
        ConsentText.objects.filter(
            clinic_id=session.clinic_id,
            purpose=ConsentText.Purpose.TELECONSULTATION,
        )
        .order_by("-version")
        .first()
    )
    return current is not None and current.pk == acceptance.text_id


def _liveness_failure(session: TeleconsultSession) -> str | None:
    """Return the fixed failure reason when the session can no longer live."""
    scope = _session_scope(session.pk)
    if scope is None or scope.encounter_state != Encounter.State.OPEN:
        return "encounter_closed"
    if not _consent_is_active(session):
        return "consent_revoked"
    if _room_state(session.pk) in (
        IntegrationOperation.Status.FAILED,
        IntegrationOperation.Status.CANCELLED,
    ):
        return "room_unavailable"
    return None


def _fail(session_id: UUID, reason: str) -> None:
    """Persist one terminal failure through the resolver-owned transition.

    The guarded update runs as ``clinic_resolver`` so it commits under
    staff, patient or outbox authority alike; concurrent calls are
    idempotent because the row lock serializes them and terminal states
    return early.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.teleconsult_fail(%s, %s)",
            [str(session_id), reason],
        )


def refresh_session(session: TeleconsultSession) -> TeleconsultSession:
    """Apply stored authority to a live session; terminal states never reopen."""
    if session.state in _TERMINAL_STATES:
        return session
    session = TeleconsultSession.objects.get(pk=session.pk)
    if session.state in _TERMINAL_STATES:
        return session
    reason = _liveness_failure(session)
    if reason is not None:
        _fail(session.pk, reason)
        session = TeleconsultSession.objects.get(pk=session.pk)
    return session


def derived_state(session: TeleconsultSession) -> str:
    """Compute the honest display state without mutating stored rows."""
    if session.state in _TERMINAL_STATES:
        return session.state
    return "failed" if _liveness_failure(session) is not None else session.state


def create_session(*, clinic_id: UUID, encounter_id: UUID) -> TeleconsultSession:
    """Bind one session to its encounter, physician, patient and consent version.

    Retries converge on the stored session; a terminal session is returned
    unchanged and never reopens. Room creation is enqueued through the
    committed outbox inside the same tenant transaction.
    """
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise TeleconsultAccessDeniedError
    try:
        actor = require_current_actor_clinic_roles(
            clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError:
        _denied(encounter.pk, clinic_id, "role_denied")
    if actor != encounter.physician_id:
        _denied(encounter.pk, clinic_id, "not_assigned")
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=encounter.patient_id
    ).first()
    if enrollment is None:
        _denied(encounter.pk, clinic_id, "not_enrolled")
    with transaction.atomic():
        _lock_session(encounter.pk)
        existing = TeleconsultSession.objects.filter(encounter=encounter).first()
        if existing is not None:
            return existing
        if encounter.state != Encounter.State.OPEN:
            raise TeleconsultConflictError(_ENCOUNTER_CLOSED)
        consent = consent_for_future_use(
            clinic_id=clinic_id,
            enrollment_id=enrollment.pk,
            purpose=ConsentText.Purpose.TELECONSULTATION,
        )
        if consent is None:
            raise TeleconsultConflictError(_CONSENT_REQUIRED)
        session = TeleconsultSession.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            encounter=encounter,
            appointment_id=encounter.appointment_id,
            patient_id=encounter.patient_id,
            physician_id=actor,
            consent=consent,
        )
        operation_id = enqueue_operation(
            OperationRequest(
                channel=IntegrationOperation.Channel.VIDEO,
                provider=PROVIDER,
                clinic_id=clinic_id,
                subject_type=SUBJECT_TYPE,
                subject_id=session.pk,
                idempotency_key=uuid5(
                    _IDEMPOTENCY_NAMESPACE, f"teleconsult-room:{session.pk}"
                ),
                max_attempts=3,
            )
        )
        TeleconsultRoom.objects.create(
            organization_id=session.organization_id,
            operation_id=operation_id,
            session=session,
            room_name=f"tc-{session.pk}",
        )
        _event(session, "created")
        record_phase1_event(
            "teleconsult.session.created",
            clinic_id=clinic_id,
            affected_record_id=session.pk,
        )
        return session


def session_room_eligible(scope: OperationScope) -> bool:
    """Recheck stored session authority immediately before room creation.

    Registered for ``SUBJECT_TYPE``; a revoked consent, closed encounter or
    non-waiting session cancels the operation without any external effect.
    """
    room = (
        TeleconsultRoom.objects.filter(operation_id=scope.operation_id)
        .select_related("session", "operation")
        .first()
    )
    if room is None:
        return False
    session = room.session
    return (
        session.organization_id == scope.organization_id
        and session.clinic_id == scope.clinic_id
        and room.operation.subject_id == session.pk
        and room.operation.subject_type == SUBJECT_TYPE
        and session.state == TeleconsultSession.State.WAITING
        and _liveness_failure(session) is None
    )


def _issue_credential(
    session: TeleconsultSession, role: str, participant_id: UUID
) -> IssuedCredential:
    """Rotate the role's live credential; the raw token leaves only once."""
    TeleconsultCredential.objects.filter(
        session=session, role=role, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
    token = secrets.token_urlsafe(TOKEN_BYTES)
    credential = TeleconsultCredential.objects.create(
        organization_id=session.organization_id,
        session=session,
        role=role,
        participant_id=participant_id,
        token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        expires_at=timezone.now() + CREDENTIAL_TTL,
    )
    return IssuedCredential(credential=credential, token=token)


def request_physician_join(*, clinic_id: UUID, session_id: UUID) -> IssuedCredential:
    """Issue the assigned physician's short-lived room credential.

    A discovered liveness failure is persisted in its own committed
    transaction before the conflict is raised; it is never rolled back
    with the denied mutation.
    """
    session = _staff_session(clinic_id, session_id)
    _assigned_physician(session)
    session = refresh_session(session)
    if session.state not in _LIVE_STATES:
        raise TeleconsultConflictError(_SESSION_CLOSED)
    died = False
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state in _TERMINAL_STATES:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        died = _liveness_failure(session) is not None
        if not died and not _room_ready(session.pk):
            raise TeleconsultConflictError(_ROOM_NOT_READY)
        if not died:
            return _issue_credential(
                session, TeleconsultCredential.Role.PHYSICIAN, session.physician_id
            )
    refresh_session(session)
    raise TeleconsultConflictError(_SESSION_CLOSED)


def request_patient_join(*, session_id: UUID) -> IssuedCredential:
    """Issue the bound patient's short-lived room credential.

    Patient authority comes from the validated session row, never from
    caller-supplied identifiers; the session must belong to the bound
    enrollment's patient and clinic.
    """
    authority = patient_authority()
    session = TeleconsultSession.objects.filter(pk=session_id).first()
    if session is None or (
        session.patient_id != authority.patient_id
        or session.clinic_id != authority.clinic_id
        or session.organization_id != authority.organization_id
    ):
        raise PatientAccessDeniedError
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state in _TERMINAL_STATES:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        denied = _liveness_failure(session) is not None
        if not denied and not _room_ready(session.pk):
            raise TeleconsultConflictError(_ROOM_NOT_READY)
        if not denied:
            return _issue_credential(
                session, TeleconsultCredential.Role.PATIENT, authority.patient_id
            )
    refresh_session(session)
    _event(session, "join_denied", actor_role="patient")
    raise TeleconsultAccessDeniedError


def _credential_for_token(token: str) -> TeleconsultCredential | None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return (
        TeleconsultCredential.objects.filter(token_digest=digest)
        .select_related("session")
        .first()
    )


def _enter_as_physician(credential: TeleconsultCredential) -> None:
    session = credential.session
    try:
        actor = require_current_actor_clinic_roles(
            session.clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError:
        _denied(session.pk, session.clinic_id, "role_denied")
    if actor != session.physician_id or credential.participant_id != actor:
        _denied(session.pk, session.clinic_id, "not_assigned")


def _enter_as_patient(credential: TeleconsultCredential) -> None:
    authority = patient_authority()
    session = credential.session
    if (
        credential.participant_id != authority.patient_id
        or session.patient_id != authority.patient_id
        or session.clinic_id != authority.clinic_id
        or session.organization_id != authority.organization_id
    ):
        raise PatientAccessDeniedError


def enter_room(*, token: str, role: str) -> RoomEntry:
    """Exchange one live credential for the room it is bound to.

    The room is resolved only through the stored credential and session;
    expired, revoked, foreign-role or foreign-participant tokens fail
    closed, and terminal sessions can never be reopened.
    """
    if role not in TeleconsultCredential.Role.values or not token:
        raise TeleconsultAccessDeniedError
    credential = _credential_for_token(token)
    if credential is None or credential.role != role:
        raise TeleconsultAccessDeniedError
    if role == TeleconsultCredential.Role.PHYSICIAN:
        _enter_as_physician(credential)
    else:
        _enter_as_patient(credential)
    session = credential.session
    denied = False
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state in _TERMINAL_STATES:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        credential = TeleconsultCredential.objects.get(pk=credential.pk)
        denied = (
            credential.revoked_at is not None
            or credential.expires_at <= timezone.now()
            or _liveness_failure(session) is not None
        )
        if not denied and not _room_ready(session.pk):
            raise TeleconsultConflictError(_ROOM_NOT_READY)
        if not denied:
            if credential.first_used_at is None:
                credential.first_used_at = timezone.now()
                credential.save(update_fields=("first_used_at",))
                _event(session, "joined", actor_role=role)
            room = TeleconsultRoom.objects.get(session=session)
    if denied:
        refresh_session(session)
        _event(session, "join_denied", actor_role=role)
        raise TeleconsultAccessDeniedError
    return RoomEntry(
        session=session,
        room_name=room.room_name,
        role=role,
        credential=credential,
    )


def start_consultation(*, clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
    """Move a ready waiting session to active; retries return the first result."""
    session = _staff_session(clinic_id, session_id)
    _assigned_physician(session)
    session = refresh_session(session)
    if session.state not in _LIVE_STATES:
        raise TeleconsultConflictError(_SESSION_CLOSED)
    died = False
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state == TeleconsultSession.State.ACTIVE:
            return session
        if session.state in _TERMINAL_STATES:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        died = _liveness_failure(session) is not None
        if not died and not _room_ready(session.pk):
            raise TeleconsultConflictError(_ROOM_NOT_READY)
        if not died:
            session.state = TeleconsultSession.State.ACTIVE
            session.started_at = timezone.now()
            session.revision += 1
            session.save(update_fields=("state", "started_at", "revision"))
            _event(session, "started", actor_role="physician")
            record_phase1_event(
                "teleconsult.session.started",
                clinic_id=clinic_id,
                affected_record_id=session.pk,
            )
            return session
    refresh_session(session)
    raise TeleconsultConflictError(_SESSION_CLOSED)


def end_consultation(*, clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
    """End a live session once and revoke every outstanding credential."""
    session = _staff_session(clinic_id, session_id)
    _assigned_physician(session)
    session = refresh_session(session)
    if session.state == TeleconsultSession.State.FAILED:
        raise TeleconsultConflictError(_SESSION_CLOSED)
    died = False
    with transaction.atomic():
        _lock_session(session.pk)
        session = TeleconsultSession.objects.get(pk=session.pk)
        if session.state == TeleconsultSession.State.ENDED:
            return session
        if session.state == TeleconsultSession.State.FAILED:
            raise TeleconsultConflictError(_SESSION_CLOSED)
        died = _liveness_failure(session) is not None
        if not died:
            session.state = TeleconsultSession.State.ENDED
            session.ended_at = timezone.now()
            session.revision += 1
            session.save(update_fields=("state", "ended_at", "revision"))
            TeleconsultCredential.objects.filter(
                session=session, revoked_at__isnull=True
            ).update(revoked_at=session.ended_at)
            _event(session, "ended", actor_role="physician")
            record_phase1_event(
                "teleconsult.session.ended",
                clinic_id=clinic_id,
                affected_record_id=session.pk,
            )
            return session
    refresh_session(session)
    raise TeleconsultConflictError(_SESSION_CLOSED)


def assigned_session(*, clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
    """Resolve one session for its bound physician only; others are denied.

    The clinician workspace re-validates this on every note action so a
    session identifier never becomes authority over another physician's
    encounter or another patient's record.
    """
    session = _staff_session(clinic_id, session_id)
    _assigned_physician(session)
    return session


def patient_joined(session: TeleconsultSession) -> bool:
    """Derive the patient's presence from the scoped event history only."""
    return session.events.filter(kind="joined", actor_role="patient").exists()


def staff_sessions(*, clinic_id: UUID) -> list[TeleconsultSession]:
    """List this clinic's sessions for the provider workspace only."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    return list(
        TeleconsultSession.objects.filter(clinic_id=clinic_id)
        .select_related("encounter", "patient")
        .order_by("-created_at", "pk")
    )


def open_encounters(*, clinic_id: UUID) -> list[Encounter]:
    """List this physician's open encounters that may start a session."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.PHYSICIAN,)
    )
    return list(
        Encounter.objects.filter(
            clinic_id=clinic_id,
            physician_id=actor,
            state=Encounter.State.OPEN,
            teleconsultsession__isnull=True,
        )
        .select_related("patient", "appointment")
        .order_by("appointment__start_at", "pk")
    )


def session_events(*, clinic_id: UUID, session_id: UUID) -> list[TeleconsultEvent]:
    """Read the scoped event history for one session in this clinic."""
    session = _staff_session(clinic_id, session_id)
    return list(session.events.order_by("created_at", "pk"))


def patient_sessions() -> list[TeleconsultSession]:
    """List only the sessions bound to this patient's enrollment."""
    authority = patient_authority()
    return list(
        TeleconsultSession.objects.filter(
            clinic_id=authority.clinic_id, patient_id=authority.patient_id
        ).order_by("-created_at", "pk")
    )


def patient_session_events(*, session_id: UUID) -> list[TeleconsultEvent]:
    """Read scoped history only for a session bound to this patient."""
    authority = patient_authority()
    session = TeleconsultSession.objects.filter(pk=session_id).first()
    if session is None or (
        session.patient_id != authority.patient_id
        or session.clinic_id != authority.clinic_id
    ):
        raise PatientAccessDeniedError
    return list(session.events.order_by("created_at", "pk"))


def live_credential(
    *, session: TeleconsultSession, role: str
) -> TeleconsultCredential | None:
    """Return the role's currently usable entered credential, if one exists."""
    return (
        TeleconsultCredential.objects.filter(
            session=session,
            role=role,
            revoked_at__isnull=True,
            first_used_at__isnull=False,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )
