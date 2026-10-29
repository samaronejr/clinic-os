"""Persisted exact-session step-up and scoped alias projections."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import connection, transaction

from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.identity.models import Clinic, SessionRecord, UserClinicRole
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    require_verified_session,
    session_hash,
)
from apps.identity.stepup import StepUpRequired
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

MERGE_ROLES: Final = (
    UserClinicRole.Role.CLINIC_MANAGER,
    UserClinicRole.Role.PHYSICIAN,
    # These are the established legacy clinic-manager assignments.
    UserClinicRole.Role.CLINIC_ADMIN,
    UserClinicRole.Role.OWNER,
)
SESSION_HASH_HEX_LENGTH: Final = 64


class DuplicateInputError(ValueError):
    """Refuse malformed adjudication without reflecting identifying values."""

    def __init__(self) -> None:
        """Expose one fixed input error."""
        super().__init__("duplicate review input is invalid")


class DuplicateConflictError(Exception):
    """Expose only a closed, non-identifying state conflict code."""

    def __init__(self, code: str) -> None:
        """Retain the machine-consumed conflict without user content."""
        self.code = code
        super().__init__(code)


class PatientAliasedError(ValueError):
    """Require a surviving registration for a genuinely new operation."""

    code: Final = "patient_aliased"

    def __init__(self) -> None:
        """Expose the plan's fixed conflict code."""
        super().__init__(self.code)


class MergeCrossOrganizationError(PatientAccessDeniedError):
    """Unknown, foreign and mismatched merge selectors share one refusal."""

    code: Final = "merge_cross_org"

    def __init__(self) -> None:
        """Return the same fixed code without disclosing the record's realm."""
        super().__init__()


@dataclass(frozen=True, slots=True)
class AliasProjection:
    """Original retained identities and the current surviving registration."""

    survivor_patient_id: UUID
    patient_ids: tuple[UUID, ...]
    unified_at: datetime | None


def review_actor(clinic_id: UUID) -> tuple[UUID, Clinic]:
    """Require current exact-clinic human adjudication authority."""
    try:
        actor = require_current_actor_clinic_roles(clinic_id, MERGE_ROLES)
        require_permission("demographics.read", clinic_id=clinic_id)
        clinic = Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise PatientAccessDeniedError from error
    return UUID(str(actor)), clinic


@contextmanager
def duplicate_review_session(*, clinic_id: UUID, session_key: str) -> Iterator[None]:
    """Bind a genuine persisted fresh session, never a caller-supplied Boolean.

    The HTTP layer supplies its own framework session key. Services and
    triggers revalidate the hash against the session inventory, actor,
    device, expiry and revocation; the key is neither stored nor audited.
    """
    review_actor(clinic_id)
    if not connection.in_atomic_block:
        raise StepUpRequired
    try:
        require_verified_session(session_key=session_key)
    except (SessionInventoryDeniedError, UnicodeEncodeError) as error:
        raise StepUpRequired from error
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_merge_session', true)")
        previous = cursor.fetchone()
        cursor.execute(
            "SELECT set_config('app.current_merge_session', %s, true)",
            [session_hash(session_key).hex()],
        )
    try:
        with transaction.atomic():
            yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_merge_session', %s, true)",
                [previous[0] or "" if previous else ""],
            )


def verified_review_session(actor_id: UUID) -> SessionRecord:
    """Revalidate the exact live proof at the service's mutation boundary."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_merge_session', true)")
        row = cursor.fetchone()
        digest = row[0] if row else None
        if (
            not isinstance(digest, str)
            or len(digest) != SESSION_HASH_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise StepUpRequired
        cursor.execute(
            "SELECT clinic_app.identity_session_verified(%s)",
            [bytes.fromhex(digest)],
        )
        if cursor.fetchone() != (True,):
            raise StepUpRequired
    session = SessionRecord.objects.filter(
        user_id=actor_id, session_key_hash=bytes.fromhex(digest)
    ).first()
    if session is None or session.verified_at is None:
        raise StepUpRequired
    return session


def patient_alias_set(*, clinic_id: UUID, patient_id: UUID) -> AliasProjection:
    """Resolve same-clinic enrollment metadata without bypassing domain RLS."""
    try:
        actor_id = current_actor_id()
    except CurrentActorError as error:
        raise PatientAccessDeniedError from error
    if not UserClinicRole.objects.filter(
        clinic_id=clinic_id, user_id=actor_id
    ).exists():
        raise PatientAccessDeniedError
    if not PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=patient_id
    ).exists():
        raise PatientAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT patient_id, survivor_patient_id, unified_at "
            "FROM clinic_app.patient_alias_set(%s)",
            [patient_id],
        )
        rows = cursor.fetchall()
    if not rows:
        raise PatientAccessDeniedError
    return AliasProjection(rows[0][1], tuple(row[0] for row in rows), rows[0][2])


def require_survivor(*, clinic_id: UUID, patient_id: UUID) -> None:
    """Reject only an alias; callers retain their original enrollment validation."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.survivor_patient_id FROM clinic_app.patient_alias_set(%s) a "
            "WHERE EXISTS ("
            "SELECT 1 FROM clinic_app.intake_patientclinicenrollment e "
            "WHERE e.clinic_id=%s AND e.patient_id=%s "
            "UNION ALL SELECT 1 FROM clinic_app.patient_booking_scope() s "
            "WHERE s.clinic_id=%s AND s.patient_id=%s) LIMIT 1",
            [patient_id, clinic_id, patient_id, clinic_id, patient_id],
        )
        patient_row = cursor.fetchone()
    if patient_row is not None and patient_row[0] != patient_id:
        raise PatientAliasedError


def patient_alias_overview() -> datetime | None:
    """Read the unified-date marker only from the currently live portal session."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.patient_alias_overview()")
        row = cursor.fetchone()
    return row[0] if row else None
