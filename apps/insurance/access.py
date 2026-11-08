"""Clinic-scoped authority, serialization and canonical replay binding."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import UUID

import rfc8785
from django.db import DatabaseError, IntegrityError, connection, transaction
from psycopg import Error as PostgresError

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.insurance.errors import (
    InsuranceConflictError,
    InsuranceDeniedError,
    InsuranceInputError,
)
from apps.insurance.models import InsuranceRow

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.audit.canonical import CanonicalValue


def fingerprint(value: object) -> str:
    """Retain the digest internally, never in telemetry."""
    try:
        return sha256(rfc8785.dumps(cast("CanonicalValue", value))).hexdigest()
    except (TypeError, ValueError) as error:
        raise InsuranceInputError from error


def authority(*, clinic_id: UUID, permission: str) -> UUID:
    """Unknown selectors and other clinics have the same standard guard failure."""
    if type(clinic_id) is not UUID:
        raise InsuranceDeniedError
    try:
        return UUID(str(require_permission(permission, clinic_id=clinic_id)))
    except CurrentActorError as error:
        raise InsuranceDeniedError from error


@contextmanager
def clinic_write(
    *, clinic_id: UUID, permission: str = "tiss.manage"
) -> Iterator[Clinic]:
    """Serialize insurance commands before loading mutable state or replay receipts."""
    with transaction.atomic():
        authority(clinic_id=clinic_id, permission=permission)
        try:
            clinic = Clinic.objects.get(pk=clinic_id)
        except Clinic.DoesNotExist as error:
            raise InsuranceDeniedError from error
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                [f"insurance-clinic-v1:{clinic_id}"],
            )
        try:
            with transaction.atomic():
                yield clinic
        except IntegrityError as error:
            raise InsuranceConflictError from error
        except DatabaseError as error:
            cause = error.__cause__
            if isinstance(cause, PostgresError) and cause.sqlstate == "42501":
                raise InsuranceDeniedError from error
            raise


def scoped[T: InsuranceRow](*, model: type[T], clinic_id: UUID, identifier: UUID) -> T:
    """Keep RLS and explicit exact-clinic binding in every record selection."""
    try:
        return model.objects.get(pk=identifier, clinic_id=clinic_id)
    except model.DoesNotExist as error:
        raise InsuranceDeniedError from error


def replay[T: InsuranceRow](
    *, model: type[T], clinic_id: UUID, command_id: UUID, digest: str
) -> T | None:
    """Replays must match the full immutable command, including the selected subject."""
    if type(command_id) is not UUID:
        raise InsuranceInputError
    row = model.objects.filter(clinic_id=clinic_id, command_id=command_id).first()
    if row is not None and row.fingerprint != digest:
        raise InsuranceConflictError
    return row


def audit(*, clinic_id: UUID, row: InsuranceRow, event: str) -> None:
    """No application payload can extend the fixed metadata-only audit vocabulary."""
    record_phase1_event(event, clinic_id=clinic_id, affected_record_id=row.pk)
