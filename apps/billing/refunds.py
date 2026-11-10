"""Reserve refunds and chargebacks before any synthetic dispatch transition."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.journal import audit_postings
from apps.billing.models import JournalSettlement, Refund, SettlementAllocation
from apps.billing.money_types import (
    JournalConflictError,
    JournalInputError,
    minor_amount,
)
from apps.billing.security import (
    book_write,
    request_actor,
    require_finance_step_up,
    require_money_rehearsal,
)

if TYPE_CHECKING:
    from uuid import UUID


def request_refund(
    *,
    clinic_id: UUID,
    allocation_id: UUID,
    amount_minor: int,
    command_id: UUID,
    kind: str = "refund",
) -> Refund:
    """Reserve an allocated verified collection; the database serializes all claims."""
    minor_amount(amount_minor)
    actor = request_actor(clinic_id=clinic_id, kind="refund")
    if kind not in {"refund", "chargeback"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="charge.read") as book:
        try:
            allocation = SettlementAllocation.objects.get(pk=allocation_id, book=book)
        except SettlementAllocation.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        existing = Refund.objects.filter(book=book, command_id=command_id).first()
        if existing is not None:
            if (existing.allocation_id, existing.amount_minor, existing.kind) != (
                allocation_id,
                amount_minor,
                kind,
            ):
                raise JournalConflictError
            return existing
        refund = Refund.objects.create(
            organization_id=book.organization_id,
            book=book,
            allocation=allocation,
            settlement_id=allocation.settlement_id,
            amount_minor=amount_minor,
            command_id=command_id,
            requested_by_id=actor,
            kind=kind,
        )
        record_phase1_event(
            "billing.refund.requested",
            clinic_id=clinic_id,
            affected_record_id=refund.pk,
        )
        return refund


def request_unallocated_refund(
    *,
    clinic_id: UUID,
    settlement_id: UUID,
    amount_minor: int,
    command_id: UUID,
) -> Refund:
    """Reserve an overpayment, advance or unidentified receipt."""
    minor_amount(amount_minor)
    actor = request_actor(clinic_id=clinic_id, kind="refund")
    with book_write(clinic_id=clinic_id, permission="charge.read") as book:
        if not JournalSettlement.objects.filter(pk=settlement_id, book=book).exists():
            raise BillingAccessDeniedError
        existing = Refund.objects.filter(book=book, command_id=command_id).first()
        if existing is not None:
            if (
                existing.settlement_id,
                existing.allocation_id,
                existing.amount_minor,
            ) != (
                settlement_id,
                None,
                amount_minor,
            ):
                raise JournalConflictError
            return existing
        refund = Refund.objects.create(
            organization_id=book.organization_id,
            book=book,
            settlement_id=settlement_id,
            amount_minor=amount_minor,
            command_id=command_id,
            requested_by_id=actor,
        )
        record_phase1_event(
            "billing.refund.requested",
            clinic_id=clinic_id,
            affected_record_id=refund.pk,
        )
        return refund


def approve_refund(*, clinic_id: UUID, refund_id: UUID) -> Refund:
    """Finance and genuine persisted step-up bind approval to exact immutable terms."""
    with book_write(clinic_id=clinic_id, permission="refund.approve") as book:
        require_finance_step_up(clinic_id=clinic_id)
        try:
            refund = Refund.objects.get(pk=refund_id, book=book)
        except Refund.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        if refund.state != "requested":
            if refund.state in {"approved", "sending", "confirmed", "unknown"}:
                return refund
            raise JournalConflictError
        refund.state = "approved"
        refund.save(update_fields=("state",))
        refund.refresh_from_db()
        record_phase1_event(
            "billing.refund.approved", clinic_id=clinic_id, affected_record_id=refund.pk
        )
        audit_postings(
            clinic_id=clinic_id, source_id=refund.pk, kinds=("refund_approved",)
        )
        return refund


def transition_refund(
    *,
    clinic_id: UUID,
    refund_id: UUID,
    next_state: str,
    evidence_reference: UUID | None = None,
) -> Refund:
    """Rehearse sending or record a separately evidenced outcome; no provider call."""
    require_money_rehearsal()
    if next_state not in {"sending", "confirmed", "rejected", "unknown"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="refund.approve") as book:
        require_finance_step_up(clinic_id=clinic_id)
        try:
            refund = Refund.objects.get(pk=refund_id, book=book)
        except Refund.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        if refund.state == next_state:
            if refund.evidence_reference != evidence_reference:
                raise JournalConflictError
            return refund
        refund.state = next_state
        refund.evidence_reference = evidence_reference
        refund.save(update_fields=("state", "evidence_reference"))
        refund.refresh_from_db()
        record_phase1_event(
            f"billing.refund.{next_state}",
            clinic_id=clinic_id,
            affected_record_id=refund.pk,
        )
        audit_postings(
            clinic_id=clinic_id,
            source_id=refund.pk,
            kinds=(f"refund_{next_state}", "repasse_reversal"),
        )
        return refund
