"""Enrollment-bound patient invitations and short-lived patient sessions.

Staff issue a single-use invitation for one existing enrollment; only the
SHA-256 hash of the random code is stored. Redemption is a POST-body code
exchange executed by ``clinic_app.redeem_patient_invitation``, which
atomically consumes the grant and mints a session row bound server-side to
the organization, clinic, patient, enrollment and granted operations.

Patient requests never enter the staff tenant context: the session is
validated by ``clinic_app.touch_patient_session`` inside its own
transaction and every patient read goes through ``SECURITY DEFINER``
resolvers that re-validate the session row, so the runtime role holds no
INSERT on sessions and no tenant GUC is ever set for a patient. Policy
defaults live in ``docs/clinical/patient-access.md``.
"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.core.secrets import secret_store
from apps.identity.current_context import CurrentActorError, current_actor_username
from apps.intake.access import PatientAccessDeniedError, authorized_enrollment
from apps.intake.models import (
    PATIENT_OPERATION_VALUES,
    PatientAccessGrant,
    PatientSession,
)
from apps.tenancy.db import (
    TenantTransactionNestingError,
    clear_connection_tenant_gucs,
)
from apps.tenancy.envelope import KEK_SECRET_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator

INVITATION_TTL: Final = timedelta(hours=24)
SESSION_ABSOLUTE_TTL: Final = timedelta(hours=8)
SESSION_IDLE_TTL: Final = timedelta(minutes=30)
SECRET_BYTES: Final = 32
MAX_ACTOR_LABEL_LENGTH: Final = 150
PATIENT_SESSION_KEY: Final = "patient_session_id"
BINDING_COLUMN_COUNT: Final = 8
OVERVIEW_COLUMN_COUNT: Final = 7


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """The one-time secret shown to staff exactly once."""

    grant: PatientAccessGrant
    secret: str


@dataclass(frozen=True, slots=True)
class PatientSessionBinding:
    """The server-side binding a validated patient session carries."""

    session_id: UUID
    organization_id: UUID
    clinic_id: UUID
    patient_id: UUID
    enrollment_id: UUID
    operations: tuple[str, ...]
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class PatientSessionOverview:
    """Everything the patient home screen renders for the bound enrollment."""

    session_id: UUID
    patient_name: str
    clinic_name: str
    enrolled_at: datetime
    operations: tuple[str, ...]
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class GrantView:
    """One invitation row for the staff access screen."""

    grant_id: UUID
    issued_by_label: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionView:
    """One patient session row for the staff access screen."""

    session_id: UUID
    grant_id: UUID
    created_at: datetime
    expires_at: datetime
    idle_expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccessOverview:
    """Everything the staff access screen renders for one enrollment."""

    enrollment_id: UUID
    patient_display_name: str
    grants: tuple[GrantView, ...]
    sessions: tuple[SessionView, ...]


def _actor_label() -> str:
    """Capture the current actor's username through the trusted resolver."""
    try:
        return current_actor_username()[:MAX_ACTOR_LABEL_LENGTH]
    except CurrentActorError as error:
        raise PatientAccessDeniedError from error


def _secret_hash(secret: str) -> bytes:
    """Return the stored digest of one invitation code."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def issue_invitation(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
) -> IssuedInvitation:
    """Issue one 24-hour single-use invitation for an existing enrollment.

    The random secret is returned for the one-time display; only its hash
    is persisted. Issuing never revokes earlier outstanding invitations.
    """
    with transaction.atomic():
        actor_id, clinic, enrollment = authorized_enrollment(clinic_id, enrollment_id)
        secret = secrets.token_urlsafe(SECRET_BYTES)
        grant = PatientAccessGrant.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic.pk,
            patient_id=enrollment.patient_id,
            enrollment_id=enrollment.pk,
            issued_by_id=actor_id,
            issued_by_label=_actor_label(),
            secret_hash=_secret_hash(secret),
            operations=list(PATIENT_OPERATION_VALUES),
            expires_at=timezone.now() + INVITATION_TTL,
        )
        record_phase1_event(
            "intake.patient_access.issued",
            clinic_id=clinic.pk,
            affected_record_id=grant.pk,
        )
        return IssuedInvitation(grant=grant, secret=secret)


def access_overview(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
) -> AccessOverview:
    """Return the invitation and session state for one enrolled patient."""
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment(clinic_id, enrollment_id)
        grants = tuple(
            GrantView(
                grant_id=grant.pk,
                issued_by_label=grant.issued_by_label,
                created_at=grant.created_at,
                expires_at=grant.expires_at,
                consumed_at=grant.consumed_at,
                revoked_at=grant.revoked_at,
            )
            for grant in PatientAccessGrant.objects.filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                enrollment_id=enrollment.pk,
            ).order_by("-created_at", "pk")
        )
        sessions = tuple(
            SessionView(
                session_id=session.pk,
                grant_id=session.grant_id,
                created_at=session.created_at,
                expires_at=session.expires_at,
                idle_expires_at=session.idle_expires_at,
                revoked_at=session.revoked_at,
            )
            for session in PatientSession.objects.filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                enrollment_id=enrollment.pk,
            ).order_by("-created_at", "pk")
        )
        record_phase1_event(
            "intake.patient_access.viewed",
            clinic_id=clinic.pk,
            affected_record_id=enrollment.pk,
        )
        return AccessOverview(
            enrollment_id=enrollment.pk,
            patient_display_name=enrollment.patient.full_name,
            grants=grants,
            sessions=sessions,
        )


def revoke_patient_access(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    grant_id: UUID,
) -> None:
    """Revoke one invitation and every session minted from it.

    The grant must belong to the authorized clinic enrollment; anything
    else is the same non-identifying denial.
    """
    if type(grant_id) is not UUID:
        raise PatientAccessDeniedError
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment(clinic_id, enrollment_id)
        grant = (
            PatientAccessGrant.objects.select_for_update()
            .filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                enrollment_id=enrollment.pk,
                pk=grant_id,
            )
            .first()
        )
        if grant is None:
            raise PatientAccessDeniedError
        now = timezone.now()
        if grant.revoked_at is None:
            grant.revoked_at = now
            grant.save(update_fields=("revoked_at",))
            record_phase1_event(
                "intake.patient_access.revoked",
                clinic_id=clinic.pk,
                affected_record_id=grant.pk,
            )
        PatientSession.objects.filter(
            organization_id=clinic.organization_id,
            grant_id=grant.pk,
            revoked_at__isnull=True,
        ).update(revoked_at=now)


def redeem_invitation(clinic_id: UUID, code: str) -> UUID | None:
    """Exchange one invitation code for a new patient session id.

    The code travels only in the POST body; the clinic in the redemption
    path is part of the lookup, so a code issued for another clinic fails
    identically to an unknown, expired, consumed or revoked code. Returns
    the new session id or ``None`` for every failure.
    """
    if type(clinic_id) is not UUID or type(code) is not str or not code.strip():
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.redeem_patient_invitation(%s, %s)",
            [str(clinic_id), _secret_hash(code)],
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], UUID):
        return None
    return row[0]


def _binding_from_row(row: tuple[object, ...] | None) -> PatientSessionBinding | None:
    """Type-check one touch result into the session binding."""
    if row is None or len(row) != BINDING_COLUMN_COUNT:
        return None
    (
        session_id,
        organization_id,
        clinic_id,
        patient_id,
        enrollment_id,
        operations,
        expires_at,
        idle_expires_at,
    ) = row
    if (
        not isinstance(session_id, UUID)
        or not isinstance(organization_id, UUID)
        or not isinstance(clinic_id, UUID)
        or not isinstance(patient_id, UUID)
        or not isinstance(enrollment_id, UUID)
        or not isinstance(operations, list)
        or not all(type(operation) is str for operation in operations)
        or not isinstance(expires_at, datetime)
        or not isinstance(idle_expires_at, datetime)
    ):
        return None
    return PatientSessionBinding(
        session_id=session_id,
        organization_id=organization_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        enrollment_id=enrollment_id,
        operations=tuple(operations),
        expires_at=expires_at,
        idle_expires_at=idle_expires_at,
    )


@contextmanager
def patient_session_context(
    session_id: UUID,
) -> Iterator[PatientSessionBinding | None]:
    """Validate one patient session inside its own transaction.

    The boundary fails closed at entry: any persistent staff tenant GUCs
    already on the connection are reset before the session touch, so a
    reused connection can never carry staff authority into patient work.
    The database touch re-checks revocation and both expiries and renews
    the idle deadline atomically. No staff tenant GUC is set, so every
    tenant-isolation policy fails closed for the duration; patient reads
    go through resolver functions that re-validate the session row.
    """
    if connection.in_atomic_block:
        raise TenantTransactionNestingError
    clear_connection_tenant_gucs()
    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.touch_patient_session(%s)",
                [str(session_id)],
            )
            row = cursor.fetchone()
            binding = _binding_from_row(row)
            if binding is not None:
                cursor.execute(
                    "SELECT pg_catalog.set_config("
                    "'app.current_patient_session', %s, true)",
                    [str(session_id)],
                )
        yield binding


def patient_session_overview() -> PatientSessionOverview | None:
    """Return the bound enrollment view for the current patient session.

    The session id comes from the transaction-local
    ``app.current_patient_session`` GUC set by the request boundary; the
    resolver re-validates the session row on every call and decrypts the
    patient name inside its own boundary through ``protected_decrypt``,
    which binds the session's organization for the duration of the call.
    The KEK comes from the configured secret store, never from the request.
    """
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.patient_session_overview(%s)", [kek])
        row = cursor.fetchone()
    if row is None or len(row) != OVERVIEW_COLUMN_COUNT:
        return None
    (
        session_id,
        patient_name,
        clinic_name,
        enrolled_at,
        operations,
        expires_at,
        idle_expires_at,
    ) = row
    if (
        not isinstance(session_id, UUID)
        or not isinstance(patient_name, str)
        or not isinstance(clinic_name, str)
        or not isinstance(enrolled_at, datetime)
        or not isinstance(operations, list)
        or not all(type(operation) is str for operation in operations)
        or not isinstance(expires_at, datetime)
        or not isinstance(idle_expires_at, datetime)
    ):
        return None
    return PatientSessionOverview(
        session_id=session_id,
        patient_name=patient_name,
        clinic_name=clinic_name,
        enrolled_at=enrolled_at,
        operations=tuple(operations),
        expires_at=expires_at,
        idle_expires_at=idle_expires_at,
    )


def end_patient_session(session_id: UUID) -> None:
    """Revoke one session server-side; unknown ids are a no-op."""
    if type(session_id) is not UUID:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.end_patient_session(%s)",
            [str(session_id)],
        )
