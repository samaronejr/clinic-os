"""Operational largest-remainder splits with a retained exact command binding."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from django.db.models import Sum

from apps.billing.allocation_posting import AllocationMoney, post_allocation
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import (
    InvoiceTerms,
    JournalSettlement,
    Refund,
    SettlementAllocation,
)
from apps.billing.money_types import (
    JournalConflictError,
    JournalInputError,
    largest_remainder,
)
from apps.billing.security import book_write

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_TARGETS: Final = 100


def allocate_proportionally(
    *,
    clinic_id: UUID,
    settlement_id: UUID,
    invoice_ids: Sequence[UUID],
    command_id: UUID,
) -> tuple[SettlementAllocation, ...]:
    """Split verified money by outstanding net debt; retain the first split."""
    if (
        not invoice_ids
        or len(invoice_ids) > MAX_TARGETS
        or len(set(invoice_ids)) != len(invoice_ids)
        or any(type(value) is not UUID for value in invoice_ids)
        or type(command_id) is not UUID
    ):
        raise JournalInputError
    identifiers = tuple(sorted(str(value) for value in invoice_ids))
    fingerprint = hashlib.sha256(
        (
            "billing-allocation-split-v1\0"
            + str(settlement_id)
            + ":"
            + ",".join(identifiers)
        ).encode("ascii")
    ).digest()
    with book_write(clinic_id=clinic_id, permission="settlement.post") as book:
        existing = tuple(
            SettlementAllocation.objects.filter(
                book=book, group_id=command_id
            ).order_by("terms_id")
        )
        if existing:
            if any(
                bytes(row.group_fingerprint or b"") != fingerprint
                or row.settlement_id != settlement_id
                for row in existing
            ):
                raise JournalConflictError
            return existing
        try:
            settlement = JournalSettlement.objects.get(book=book, pk=settlement_id)
        except JournalSettlement.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        terms = tuple(
            InvoiceTerms.objects.filter(book=book, invoice_id__in=invoice_ids).order_by(
                "invoice_id"
            )
        )
        if len(terms) != len(invoice_ids):
            raise BillingAccessDeniedError
        if settlement.state != "posted":
            raise JournalConflictError
        spent = int(
            SettlementAllocation.objects.filter(settlement=settlement).aggregate(
                total=Sum("amount_minor")
            )["total"]
            or 0
        )
        reserved = int(
            Refund.objects.filter(settlement=settlement, allocation__isnull=True)
            .exclude(state="rejected")
            .aggregate(total=Sum("amount_minor"))["total"]
            or 0
        )
        collected = {
            row["terms_id"]: int(row["total"])
            for row in SettlementAllocation.objects.filter(terms__in=terms)
            .values("terms_id")
            .annotate(total=Sum("amount_minor"))
        }
        returned_advances = {
            row["allocation__terms_id"]: int(row["total"])
            for row in Refund.objects.filter(
                allocation__terms__in=terms,
                state="confirmed",
                approval_entry__journalline__account__code="patient_advances",
                approval_entry__journalline__side="debit",
            )
            .values("allocation__terms_id")
            .annotate(total=Sum("amount_minor"))
        }
        weights = tuple(
            (
                str(term.invoice_id),
                term.net_minor
                - collected.get(term.pk, 0)
                + returned_advances.get(term.pk, 0),
            )
            for term in terms
        )
        total = min(
            settlement.amount_minor - spent - reserved,
            sum(weight for _, weight in weights),
        )
        if total <= 0:
            raise JournalConflictError
        amounts = largest_remainder(total, weights)
        return tuple(
            post_allocation(
                book=book,
                settlement=settlement,
                terms=term,
                money=AllocationMoney(
                    amounts[str(term.invoice_id)],
                    uuid5(command_id, str(term.invoice_id)),
                    command_id,
                    fingerprint,
                ),
            )
            for term in terms
            if amounts[str(term.invoice_id)] > 0
        )
