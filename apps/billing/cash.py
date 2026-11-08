"""Observed drawer counts with revisioned close and verified reopening."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import CashSession, CashSessionEvent
from apps.billing.money_types import (
    JournalConflictError,
    JournalInputError,
    minor_amount,
)
from apps.billing.security import book_write, require_finance_step_up

if TYPE_CHECKING:
    from uuid import UUID


def open_cash(*, clinic_id: UUID, counted_minor: int, command_id: UUID) -> CashSession:
    """Assert opening cash against the journal; never manufacture an opening balance."""
    minor_amount(counted_minor, zero=True)
    with book_write(clinic_id=clinic_id, permission="charge.collect") as book:
        existing = CashSession.objects.filter(book=book, command_id=command_id).first()
        if existing is not None:
            opening = CashSessionEvent.objects.get(session=existing, revision=1)
            if opening.counted_minor != counted_minor:
                raise JournalConflictError
            return existing
        session = CashSession.objects.create(
            organization_id=book.organization_id,
            book=book,
            counted_minor=counted_minor,
            command_id=command_id,
            last_command_id=command_id,
        )
        session.refresh_from_db()
        record_phase1_event(
            "billing.cash.opened", clinic_id=clinic_id, affected_record_id=session.pk
        )
        return session


def close_cash(
    *,
    clinic_id: UUID,
    session_id: UUID,
    counted_minor: int,
    expected_revision: int,
    command_id: UUID,
) -> CashSession:
    """Close only the reviewed revision with zero unexplained difference/exceptions."""
    minor_amount(counted_minor, zero=True)
    with book_write(clinic_id=clinic_id, permission="charge.collect") as book:
        try:
            session = CashSession.objects.get(pk=session_id, book=book)
        except CashSession.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        receipt = CashSessionEvent.objects.filter(
            book=book, command_id=command_id
        ).first()
        if receipt is not None:
            if (receipt.session_id, receipt.state, receipt.counted_minor) != (
                session_id,
                "closed",
                counted_minor,
            ):
                raise JournalConflictError
            return session
        if session.revision != expected_revision or session.state != "open":
            raise JournalConflictError
        session.state, session.counted_minor, session.reason = (
            "closed",
            counted_minor,
            "",
        )
        session.last_command_id = command_id
        session.save(
            update_fields=("state", "counted_minor", "reason", "last_command_id")
        )
        session.refresh_from_db()
        record_phase1_event(
            "billing.cash.closed", clinic_id=clinic_id, affected_record_id=session.pk
        )
        return session


def reopen_cash(
    *,
    clinic_id: UUID,
    session_id: UUID,
    reason: str,
    expected_revision: int,
    command_id: UUID,
) -> CashSession:
    """Retain the close; append a reasoned reopening bound to genuine fresh step-up."""
    if reason not in {"correction", "reconciliation"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="settlement.post") as book:
        require_finance_step_up(clinic_id=clinic_id)
        try:
            session = CashSession.objects.get(pk=session_id, book=book)
        except CashSession.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        receipt = CashSessionEvent.objects.filter(
            book=book, command_id=command_id
        ).first()
        if receipt is not None:
            if (receipt.session_id, receipt.state, receipt.reason) != (
                session_id,
                "reopened",
                reason,
            ):
                raise JournalConflictError
            return session
        if session.revision != expected_revision or session.state != "closed":
            raise JournalConflictError
        session.state, session.reason, session.last_command_id = (
            "open",
            reason,
            command_id,
        )
        session.save(update_fields=("state", "reason", "last_command_id"))
        session.refresh_from_db()
        record_phase1_event(
            "billing.cash.reopened", clinic_id=clinic_id, affected_record_id=session.pk
        )
        return session
