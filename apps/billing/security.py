"""Exact-clinic financial authority and persisted exact-session step-up."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from config.settings.contracts import resolved_data_mode
from django.db import DatabaseError, IntegrityError, connection, transaction
from psycopg import Error as PostgresError

from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import JournalBook
from apps.billing.money_types import SESSION_HASH_LENGTH, JournalConflictError
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    require_verified_session,
    session_hash,
)
from apps.identity.stepup import StepUpRequired

if TYPE_CHECKING:
    from collections.abc import Iterator


class OutgoingMoneyUnavailableError(Exception):
    """A local rehearsal is not authorization to call a PSP or send live money."""

    def __init__(self) -> None:
        """Expose the fixed external-gate state."""
        super().__init__("BLOCKED-ON-EG")


def require_money_rehearsal() -> None:
    """There is no real PSP/refund/payout leg in todo 56."""
    if resolved_data_mode() != "synthetic":
        raise OutgoingMoneyUnavailableError


def request_actor(*, clinic_id: UUID, kind: str) -> UUID:
    """Allow a manager to propose or Finance to prepare its exact review request."""
    try:
        return financial_actor(clinic_id=clinic_id, permission=f"{kind}.request")
    except BillingAccessDeniedError:
        return financial_actor(clinic_id=clinic_id, permission=f"{kind}.approve")


def financial_actor(*, clinic_id: UUID, permission: str) -> UUID:
    """Revalidate the authoritative permission, never legacy manager equivalence."""
    try:
        return UUID(str(require_permission(permission, clinic_id=clinic_id)))
    except CurrentActorError as error:
        raise BillingAccessDeniedError from error


def scoped_book(*, clinic_id: UUID, permission: str) -> JournalBook:
    """Unknown and foreign clinic/book selectors have identical denials."""
    financial_actor(clinic_id=clinic_id, permission=permission)
    try:
        return JournalBook.objects.get(clinic_id=clinic_id)
    except JournalBook.DoesNotExist as error:
        raise BillingAccessDeniedError from error


def lock_clinic_book(*, clinic_id: UUID) -> None:
    """Lock before legacy invoice rows; this mutex confers no mutation authority."""
    if not connection.in_atomic_block:
        raise BillingAccessDeniedError
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.billing_lock_clinic_book(%s)", [clinic_id])


@contextmanager
def book_write(*, clinic_id: UUID, permission: str) -> Iterator[JournalBook]:
    """Order clinic, audit, book and child locks; keep failed commands in savepoints."""
    with transaction.atomic():
        book = scoped_book(clinic_id=clinic_id, permission=permission)
        with connection.cursor() as cursor:
            cursor.execute("SELECT clinic_app.billing_lock_book(%s)", [book.pk])
        try:
            with transaction.atomic():
                yield book
        except IntegrityError as error:
            raise JournalConflictError from error
        except DatabaseError as error:
            cause = error.__cause__
            if isinstance(cause, PostgresError) and cause.sqlstate == "42501":
                raise BillingAccessDeniedError from error
            raise


@contextmanager
def finance_session(*, clinic_id: UUID, session_key: str) -> Iterator[None]:
    """Bind only this framework session's persisted, fresh actual verification."""
    financial_actor(clinic_id=clinic_id, permission="charge.read")
    if not connection.in_atomic_block:
        raise StepUpRequired
    try:
        require_verified_session(session_key=session_key)
    except (SessionInventoryDeniedError, UnicodeEncodeError) as error:
        raise StepUpRequired from error
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_finance_session',true)")
        prior = cursor.fetchone()
        cursor.execute(
            "SELECT set_config('app.current_finance_session',%s,true)",
            [session_hash(session_key).hex()],
        )
    try:
        with transaction.atomic():
            yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_finance_session',%s,true)",
                [prior[0] or "" if prior else ""],
            )


def require_finance_step_up(*, clinic_id: UUID) -> UUID:
    """Recheck the stored session/device/revocation at each mutation boundary."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_finance_session',true)")
        row = cursor.fetchone()
        digest = row[0] if row else ""
        if (
            not isinstance(digest, str)
            or len(digest) != SESSION_HASH_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise StepUpRequired
        cursor.execute(
            "SELECT clinic_app.identity_session_verified(%s)", [bytes.fromhex(digest)]
        )
        if cursor.fetchone() != (True,):
            raise StepUpRequired
        cursor.execute("SELECT clinic_app.billing_finance_session(%s)", [clinic_id])
        session = cursor.fetchone()
        if session is None:
            raise StepUpRequired
        return UUID(str(session[0]))
