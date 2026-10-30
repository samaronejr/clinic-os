"""Read-only authority decisions before capture writes and audited transitions."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.core import signing
from django.db import connection

from apps.consent.services import ai_disclosure_status, consent_for_future_use
from apps.ehr.models import Encounter
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.models import PatientClinicEnrollment
from apps.scribe.contracts import Reason, ScribeError
from apps.scribe.models import RecordingSession

if TYPE_CHECKING:
    from apps.consent.models import ConsentAcceptance

TOKEN_SALT = "scribe.encounter.v1"  # noqa: S105 - signing domain, not key material
TOKEN_MAX_AGE = 6 * 60 * 60


@dataclass(frozen=True, slots=True)
class EncounterScope:
    """The encounter, subject and clinician resolved under current RLS."""

    encounter: Encounter
    enrollment: PatientClinicEnrollment
    physician_id: UUID


def encounter_scope(*, clinic_id: UUID, encounter_id: UUID) -> EncounterScope:
    """Care-team readers, administrators and unknown records cannot capture."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ScribeError
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=encounter.patient_id
    ).first()
    if enrollment is None:
        raise ScribeError
    try:
        physician = require_permission(
            "clinical.write",
            clinic_id=clinic_id,
            patient_enrollment_id=enrollment.pk,
        )
    except CurrentActorError as error:
        raise ScribeError from error
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_assigned(%s)", [encounter.pk])
        assigned = cursor.fetchone() == (True,)
    if physician != encounter.physician_id or not assigned:
        raise ScribeError
    return EncounterScope(encounter, enrollment, physician)


def lock_recording_consent(*, clinic_id: UUID) -> None:
    """Take the shared purpose lock before any recording/session row lock."""
    # Publication, acceptance and withdrawal use this same purpose lock.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"consent:{clinic_id}:consultation_recording"],
        )


def active_consent(
    scope: EncounterScope, *, lock: bool = True, acceptance_id: UUID | None = None
) -> ConsentAcceptance:
    """Require recording consent and this encounter's unrefused AI disclosure."""
    if lock:
        lock_recording_consent(clinic_id=scope.encounter.clinic_id)
    try:
        consent = consent_for_future_use(
            clinic_id=scope.encounter.clinic_id,
            enrollment_id=scope.enrollment.pk,
            purpose="consultation_recording",
        )
        disclosure = ai_disclosure_status(
            clinic_id=scope.encounter.clinic_id,
            encounter_id=scope.encounter.pk,
        )
    except CurrentActorError as error:
        raise ScribeError from error
    if (
        consent is None
        or disclosure is None
        or not disclosure.informed
        or disclosure.refused
        or (acceptance_id is not None and consent.pk != acceptance_id)
    ):
        raise ScribeError(Reason.CONSENT_REVOKED)
    return consent


def issue_encounter_token(
    *, clinic_id: UUID, encounter_id: UUID, browser_binding: str
) -> str:
    """Issue a POST-only, signed token after deciding the displayed encounter."""
    scope = encounter_scope(clinic_id=clinic_id, encounter_id=encounter_id)
    return signing.dumps(
        {
            "clinic": str(clinic_id),
            "encounter": str(encounter_id),
            "physician": str(scope.physician_id),
            "browser": sha256(browser_binding.encode()).hexdigest(),
            "nonce": uuid4().hex,
        },
        salt=TOKEN_SALT,
    )


def token_scope(*, clinic_id: UUID, token: str) -> EncounterScope:
    """Verify signature and actor without trusting identifiers inside the body."""
    try:
        terms = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        if not isinstance(terms, dict) or terms.get("clinic") != str(clinic_id):
            raise ScribeError
        scope = encounter_scope(
            clinic_id=clinic_id, encounter_id=UUID(terms["encounter"])
        )
        if terms.get("physician") != str(scope.physician_id):
            raise ScribeError
    except (signing.BadSignature, KeyError, TypeError, ValueError) as error:
        raise ScribeError from error
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.scribe_token',%s,true)",
            [sha256(token.encode()).hexdigest()],
        )
    return scope


def recording_scope(
    *, clinic_id: UUID, session_id: UUID, consent_required: bool = True
) -> tuple[RecordingSession, EncounterScope]:
    """Resolve the stored encounter and recheck assignment on every operation."""
    recording = RecordingSession.objects.filter(
        pk=session_id, clinic_id=clinic_id
    ).first()
    if recording is None:
        raise ScribeError
    scope = encounter_scope(clinic_id=clinic_id, encounter_id=recording.encounter_id)
    if scope.physician_id != recording.physician_id:
        raise ScribeError
    if consent_required:
        active_consent(scope, acceptance_id=recording.consent_id)
    return recording, scope
