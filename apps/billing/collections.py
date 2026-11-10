"""Observed, independently verified and allocated clinic collections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.billing.allocation_posting import AllocationMoney, post_allocation
from apps.billing.allocation_splits import allocate_proportionally
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.journal import audit_postings
from apps.billing.models import InvoiceTerms, JournalSettlement, SettlementAllocation
from apps.billing.money_types import (
    JournalConflictError,
    JournalInputError,
    SettlementObservation,
    minor_amount,
)
from apps.billing.security import book_write

if TYPE_CHECKING:
    from uuid import UUID

__all__ = (
    "allocate_proportionally",
    "allocate_settlement",
    "observe_settlement",
    "post_settlement",
    "quarantine_settlement",
    "verify_settlement",
)


def observe_settlement(
    *, clinic_id: UUID, observation: SettlementObservation
) -> JournalSettlement:
    """Retain evidenced money, never charge creation or synthetic success."""
    minor_amount(observation.amount_minor)
    if observation.kind not in {"patient", "insurer", "unidentified"}:
        raise JournalInputError
    if observation.account not in {"bank", "petty_cash", "psp_clearing"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="charge.collect") as book:
        row = JournalSettlement.objects.filter(
            book=book, evidence_reference=observation.evidence_reference
        ).first()
        if row is not None:
            if (row.amount_minor, row.kind, row.account, row.evidence_reference) != (
                observation.amount_minor,
                observation.kind,
                observation.account,
                observation.evidence_reference,
            ):
                raise JournalConflictError
            return row
        settlement = JournalSettlement.objects.create(
            organization_id=book.organization_id,
            book=book,
            command_id=observation.evidence_reference,
            amount_minor=observation.amount_minor,
            kind=observation.kind,
            account=observation.account,
            evidence_reference=observation.evidence_reference,
        )
        record_phase1_event(
            "billing.settlement.observed",
            clinic_id=clinic_id,
            affected_record_id=settlement.pk,
        )
        return settlement


def _settlement_state(
    *, clinic_id: UUID, settlement_id: UUID, state: str
) -> JournalSettlement:
    with book_write(clinic_id=clinic_id, permission="settlement.post") as book:
        try:
            settlement = JournalSettlement.objects.get(pk=settlement_id, book=book)
        except JournalSettlement.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        if settlement.state == state or (
            state == "verified" and settlement.state == "posted"
        ):
            return settlement
        settlement.state = state
        settlement.save(update_fields=("state",))
        # The guard supplies the verified actor/time and exact posting identity.
        settlement.refresh_from_db()
        record_phase1_event(
            f"billing.settlement.{state}",
            clinic_id=clinic_id,
            affected_record_id=settlement.pk,
        )
        if state == "posted":
            audit_postings(
                clinic_id=clinic_id, source_id=settlement.pk, kinds=("collection",)
            )
        return settlement


def verify_settlement(*, clinic_id: UUID, settlement_id: UUID) -> JournalSettlement:
    """Attest retained collection evidence without trusting a callback."""
    return _settlement_state(
        clinic_id=clinic_id, settlement_id=settlement_id, state="verified"
    )


def quarantine_settlement(*, clinic_id: UUID, settlement_id: UUID) -> JournalSettlement:
    """Retain a mismatched or unverified receipt for explicit human resolution."""
    return _settlement_state(
        clinic_id=clinic_id, settlement_id=settlement_id, state="quarantined"
    )


def post_settlement(*, clinic_id: UUID, settlement_id: UUID) -> JournalSettlement:
    """Post verified cash once to a liability; allocation never creates cash."""
    return _settlement_state(
        clinic_id=clinic_id, settlement_id=settlement_id, state="posted"
    )


def allocate_settlement(
    *,
    clinic_id: UUID,
    settlement_id: UUID,
    invoice_id: UUID,
    amount_minor: int,
    command_id: UUID,
) -> SettlementAllocation:
    """Allocate a partial, installment or deposit, with cumulative net entitlement."""
    minor_amount(amount_minor)
    with book_write(clinic_id=clinic_id, permission="settlement.post") as book:
        try:
            terms = InvoiceTerms.objects.get(book=book, invoice_id=invoice_id)
            settlement = JournalSettlement.objects.get(book=book, pk=settlement_id)
        except (InvoiceTerms.DoesNotExist, JournalSettlement.DoesNotExist) as error:
            raise BillingAccessDeniedError from error
        existing = SettlementAllocation.objects.filter(
            book=book, command_id=command_id
        ).first()
        if existing is not None:
            if (existing.settlement_id, existing.terms_id, existing.amount_minor) != (
                settlement_id,
                terms.pk,
                amount_minor,
            ):
                raise JournalConflictError
            return existing
        return post_allocation(
            book=book,
            settlement=settlement,
            terms=terms,
            money=AllocationMoney(amount_minor, command_id),
        )
