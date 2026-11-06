"""One append-only allocation write shared by explicit and proportional commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.billing.journal import audit_postings
from apps.billing.models import SettlementAllocation

if TYPE_CHECKING:
    from uuid import UUID

    from apps.billing.models import InvoiceTerms, JournalBook, JournalSettlement


@dataclass(frozen=True, slots=True)
class AllocationMoney:
    """Exact edge identity, optionally retaining its proportional-command binding."""

    amount_minor: int
    command_id: UUID
    group_id: UUID | None = None
    group_fingerprint: bytes | None = None


def post_allocation(
    *,
    book: JournalBook,
    settlement: JournalSettlement,
    terms: InvoiceTerms,
    money: AllocationMoney,
) -> SettlementAllocation:
    """Append inside the caller's already authorized and locked book transaction."""
    allocation = SettlementAllocation.objects.create(
        organization_id=book.organization_id,
        book=book,
        settlement=settlement,
        terms=terms,
        amount_minor=money.amount_minor,
        command_id=money.command_id,
        group_id=money.group_id,
        group_fingerprint=money.group_fingerprint,
    )
    allocation.refresh_from_db()
    record_phase1_event(
        "billing.allocation.created",
        clinic_id=book.clinic_id,
        affected_record_id=allocation.pk,
    )
    audit_postings(
        clinic_id=book.clinic_id, source_id=allocation.pk, kinds=("allocation",)
    )
    return allocation
