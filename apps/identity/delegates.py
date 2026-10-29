"""Verified, expiring representation commands; PostgreSQL derives authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg import Error as PostgreSQLError

from apps.audit.services import record_phase1_event
from apps.consent.models import ConsentPurpose
from apps.identity.models import PatientDelegateGrant
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PATIENT_OPERATION_VALUES

if TYPE_CHECKING:
    from collections.abc import Sequence

DELEGATE_PURPOSES: Final = frozenset({"care_processing", *ConsentPurpose.values})


class DelegateConflictError(Exception):
    """Reject a stale or illegal command without exposing the selected record."""

    def __init__(self) -> None:
        """Expose the fixed conflict code."""
        super().__init__("delegate conflict")


class DelegateInputError(ValueError):
    """Reject input outside the bounded representation contract."""

    def __init__(self) -> None:
        """Keep malformed-input errors free of identifiers or evidence content."""
        super().__init__("invalid delegate request")


@dataclass(frozen=True, slots=True)
class DelegateReceipt:
    """One retained command result; the caller cannot supply its authority."""

    grant_id: UUID
    revision: int
    state: str


@dataclass(frozen=True, slots=True)
class DelegateGrantRequest:
    """Immutable requested scope; it confers no authority before verification."""

    represented_enrollment_id: UUID
    delegate_enrollment_id: UUID
    authority_kind: str
    evidence_attachment_id: UUID
    purposes: Sequence[str]
    operations: Sequence[str]
    expires_at: datetime
    idempotency_key: UUID


def _command(
    sql: str,
    parameters: Sequence[str | int | UUID | datetime | list[str]],
    event: str,
    clinic_id: UUID,
) -> DelegateReceipt:
    """Execute a fixed resolver command and audit only its first transition."""
    with transaction.atomic():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                row = cursor.fetchone()
        except DatabaseError as error:
            cause = error.__cause__
            if isinstance(cause, PostgreSQLError):
                if cause.sqlstate == "42501":
                    raise PatientAccessDeniedError from error
                if cause.sqlstate in {"23514", "40001"}:
                    raise DelegateConflictError from error
            raise
        if row is None:
            raise PatientAccessDeniedError
        result = DelegateReceipt(row[0], row[1], row[2])
        if row[3]:
            record_phase1_event(
                event, clinic_id=clinic_id, affected_record_id=result.grant_id
            )
        return result


def request_delegate_grant(
    *, clinic_id: UUID, grant: DelegateGrantRequest
) -> DelegateReceipt:
    """Request verification; neither an invitation nor a contact grants authority."""
    if type(grant) is not DelegateGrantRequest or any(
        type(value) is not UUID
        for value in (
            clinic_id,
            grant.represented_enrollment_id,
            grant.delegate_enrollment_id,
            grant.evidence_attachment_id,
            grant.idempotency_key,
        )
    ):
        raise PatientAccessDeniedError
    if (
        grant.authority_kind not in PatientDelegateGrant.AuthorityKind.values
        or isinstance(grant.purposes, str)
        or isinstance(grant.operations, str)
        or not grant.purposes
        or not grant.operations
        or len(grant.purposes) > len(DELEGATE_PURPOSES)
        or len(grant.operations) > len(PATIENT_OPERATION_VALUES)
        or any(
            type(purpose) is not str or purpose not in DELEGATE_PURPOSES
            for purpose in grant.purposes
        )
        or any(
            type(operation) is not str or operation not in PATIENT_OPERATION_VALUES
            for operation in grant.operations
        )
        or type(grant.expires_at) is not datetime
        or timezone.is_naive(grant.expires_at)
        or grant.expires_at <= timezone.now()
    ):
        raise DelegateInputError
    return _command(
        "SELECT * FROM clinic_app.request_delegate_grant("
        "%s,%s,%s,%s,%s,%s::text[],%s::text[],%s,%s)",
        [
            clinic_id,
            grant.represented_enrollment_id,
            grant.delegate_enrollment_id,
            grant.authority_kind,
            grant.evidence_attachment_id,
            list(grant.purposes),
            list(grant.operations),
            grant.expires_at,
            grant.idempotency_key,
        ],
        "identity.delegate.requested",
        clinic_id,
    )


def _transition(
    clinic_id: UUID, grant_id: UUID, expected_revision: int, state: str, event: str
) -> DelegateReceipt:
    if type(clinic_id) is not UUID or type(grant_id) is not UUID:
        raise PatientAccessDeniedError
    if type(expected_revision) is not int or expected_revision < 1:
        raise DelegateInputError
    return _command(
        "SELECT * FROM clinic_app.transition_delegate_grant(%s,%s,%s,%s)",
        [clinic_id, grant_id, expected_revision, state],
        event,
        clinic_id,
    )


def verify_delegate_grant(
    *, clinic_id: UUID, grant_id: UUID, expected_revision: int, verified: bool
) -> DelegateReceipt:
    """Attest legal evidence independently of contact possession as verifier staff."""
    if type(verified) is not bool:
        raise DelegateInputError
    return _transition(
        clinic_id,
        grant_id,
        expected_revision,
        "verified" if verified else "rejected",
        "identity.delegate.verified" if verified else "identity.delegate.rejected",
    )


def activate(
    *, clinic_id: UUID, grant_id: UUID, expected_revision: int
) -> DelegateReceipt:
    """Activate only a verified grant; scope and the expiry never change."""
    return _transition(
        clinic_id, grant_id, expected_revision, "active", "identity.delegate.activated"
    )


def revoke(
    *, clinic_id: UUID, grant_id: UUID, expected_revision: int
) -> DelegateReceipt:
    """Revoke a nonterminal grant; bound sessions deny their next request."""
    return _transition(
        clinic_id, grant_id, expected_revision, "revoked", "identity.delegate.revoked"
    )


def switch_delegate(*, clinic_id: UUID, grant_id: UUID | None) -> None:
    """Choose an active stored grant, or return to the original enrollment."""
    if type(clinic_id) is not UUID or (
        grant_id is not None and type(grant_id) is not UUID
    ):
        raise PatientAccessDeniedError
    with transaction.atomic():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.switch_patient_delegate(%s,%s)",
                    [clinic_id, grant_id],
                )
                changed = cursor.fetchone()
                cursor.execute(
                    "SELECT NULLIF(current_setting("
                    "'app.current_patient_session',true),'')::uuid"
                )
                session = cursor.fetchone()
        except DatabaseError as error:
            cause = error.__cause__
            if isinstance(cause, PostgreSQLError) and cause.sqlstate == "42501":
                raise PatientAccessDeniedError from error
            raise
        if session is None or session[0] is None:
            raise PatientAccessDeniedError
        if changed == (True,):
            record_phase1_event(
                "identity.delegate.session_switched",
                clinic_id=clinic_id,
                affected_record_id=session[0],
            )


def request_renewal(
    *, clinic_id: UUID, idempotency_key: UUID, expires_at: datetime
) -> DelegateReceipt:
    """Clone an expired grant into pending verification; never resurrect access."""
    if type(clinic_id) is not UUID or type(idempotency_key) is not UUID:
        raise PatientAccessDeniedError
    if (
        type(expires_at) is not datetime
        or timezone.is_naive(expires_at)
        or expires_at <= timezone.now()
    ):
        raise DelegateInputError
    return _command(
        "SELECT * FROM clinic_app.renew_delegate_grant(%s,%s,%s)",
        [clinic_id, idempotency_key, expires_at],
        "identity.delegate.requested",
        clinic_id,
    )
