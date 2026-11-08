"""Exact integer read projections of posted clinic money, never stored totals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.db.models import Sum

from apps.billing.models import JournalEntry, JournalLine, Refund, SettlementAllocation

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from apps.billing.models import InvoiceTerms, JournalBook


@dataclass(frozen=True, slots=True)
class SuspenseItem:
    """One unresolved owner/date balance rather than a historic raw line."""

    owner_id: UUID
    due_on: date
    amount_minor: int


def account_balances(*, book: JournalBook) -> dict[str, int]:
    """Normalize PostgreSQL SUM(bigint)'s numeric result without rounding."""
    totals: dict[str, int] = {}
    for row in (
        JournalLine.objects.filter(entry__book=book, entry__state="posted")
        .values("account__code", "side")
        .annotate(total=Sum("amount_minor"))
    ):
        code = row["account__code"]
        amount = int(row["total"])
        totals[code] = totals.get(code, 0) + (
            amount if row["side"] == "debit" else -amount
        )
    return totals


def receivable_balances(
    *, book: JournalBook, terms: tuple[InvoiceTerms, ...]
) -> dict[UUID, int]:
    """Reconstruct frozen service debt, advance applications and release reversals."""
    entry_terms = {
        term.completion_entry_id: term.pk for term in terms if term.completion_entry_id
    }
    for allocation in SettlementAllocation.objects.filter(
        terms_id__in=[term.pk for term in terms]
    ):
        # The allocation INSERT trigger always assigns the sealed entry.
        entry_terms[cast("UUID", allocation.entry_id)] = allocation.terms_id
    returned_terms = {
        # The terms join excludes refunds without an allocation.
        row.pk: cast("SettlementAllocation", row.allocation).terms_id
        for row in Refund.objects.filter(
            allocation__terms_id__in=[term.pk for term in terms], state="rejected"
        ).select_related("allocation")
    }
    for entry in JournalEntry.objects.filter(
        book=book, source_kind="refund_rejected", source_id__in=returned_terms
    ):
        entry_terms[entry.pk] = returned_terms[entry.source_id]
    balances = dict.fromkeys((term.pk for term in terms), 0)
    for line in JournalLine.objects.filter(
        entry_id__in=entry_terms, account__code__in=("patient_ar", "insurer_ar")
    ).values("entry_id", "side", "amount_minor"):
        term_id = entry_terms[line["entry_id"]]
        balances[term_id] += (
            line["amount_minor"] if line["side"] == "debit" else -line["amount_minor"]
        )
    return balances


def suspense_items(*, book: JournalBook) -> tuple[SuspenseItem, ...]:
    """Match cash-close's owner/date buckets before any display-row limit."""
    balances: dict[tuple[UUID, date], int] = {}
    for row in (
        JournalLine.objects.filter(
            entry__book=book, entry__state="posted", account__code="suspense"
        )
        .values("owner_id", "due_on", "side")
        .annotate(total=Sum("amount_minor"))
    ):
        key = cast("UUID", row["owner_id"]), cast("date", row["due_on"])
        amount = int(row["total"])
        balances[key] = balances.get(key, 0) + (
            amount if row["side"] == "debit" else -amount
        )
    return tuple(
        SuspenseItem(owner, deadline, balances[owner, deadline])
        for owner, deadline in sorted(balances, key=lambda key: (key[1], str(key[0])))
        if balances[owner, deadline] != 0
    )
