"""Bounded clinic-only financial read models and native form choices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import connection

from apps.billing.errors import BillingAccessDeniedError
from apps.billing.finance_balances import (
    account_balances,
    receivable_balances,
    suspense_items,
)
from apps.billing.models import (
    CashSession,
    Invoice,
    InvoiceTerms,
    JournalBook,
    JournalSettlement,
    Payout,
    PractitionerPayoutRule,
    Refund,
    SettlementAllocation,
)
from apps.billing.templatetags.billing_money import minor_brl, money_state
from apps.identity.current_context import CurrentActorError, require_permission

if TYPE_CHECKING:
    from uuid import UUID

ROW_LIMIT: Final = 100
ACTION_PERMISSIONS: Final = {
    "create_book": ("finance.policy",),
    "opening_balance": ("finance.policy",),
    "create_rule": ("finance.policy",),
    "prepare_terms": ("settlement.post",),
    "complete_service": ("settlement.post",),
    "observe": ("charge.collect",),
    "verify": ("settlement.post",),
    "quarantine": ("settlement.post",),
    "post": ("settlement.post",),
    "allocate": ("settlement.post",),
    "allocate_proportionally": ("settlement.post",),
    "request_refund": ("refund.request", "refund.approve"),
    "request_unallocated_refund": ("refund.request", "refund.approve"),
    "request_payout": ("payout.request", "payout.approve"),
    "approve_refund": ("refund.approve",),
    "approve_payout": ("payout.approve",),
    "refund_state": ("refund.approve",),
    "payout_state": ("payout.approve",),
    "open_cash": ("charge.collect",),
    "close_cash": ("charge.collect",),
    "reopen_cash": ("settlement.post",),
    "psp_fee": ("settlement.post",),
}


def permits(*, clinic_id: UUID, permission: str) -> bool:
    """Mirror permission checks for presentation; services still recheck each action."""
    try:
        require_permission(permission, clinic_id=clinic_id)
    except CurrentActorError:
        return False
    return True


def finance_permissions(clinic_id: UUID) -> dict[str, bool]:
    """No clinician-only or patient identity acquires clinic-wide financial access."""
    cache = {
        permission: permits(clinic_id=clinic_id, permission=permission)
        for permission in {p for values in ACTION_PERMISSIONS.values() for p in values}
    }
    result = {
        action: any(cache[p] for p in permissions)
        for action, permissions in ACTION_PERMISSIONS.items()
    }
    if not any(result.values()):
        raise BillingAccessDeniedError
    return result


@dataclass(frozen=True, slots=True)
class Receivable:
    """A/R is projected from sealed entry sides, not invoice labels."""

    reference: UUID
    net_minor: int
    outstanding_minor: int
    completed: bool


def finance_context(
    *, clinic_id: UUID
) -> tuple[dict[str, object], dict[str, list[tuple[str, str]]]]:
    """Read recent financial records; exports cover the full date window."""
    permissions = finance_permissions(clinic_id)
    book = JournalBook.objects.filter(clinic_id=clinic_id).first()
    # The RP charges/settlements "view" cell: collection alone opens the
    # workspace's forms, never its charge, balance or journal panels.
    can_read = permits(clinic_id=clinic_id, permission="charge.read")
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "book": book,
        "permissions": permissions,
        "can_export": permits(clinic_id=clinic_id, permission="settlement.post"),
        "can_read": can_read,
    }
    if book is None:
        return context, {}
    terms = tuple(
        InvoiceTerms.objects.filter(book=book)
        .select_related("invoice")
        .order_by("-created_at", "-id")[:ROW_LIMIT]
    )
    settlements = tuple(
        JournalSettlement.objects.filter(book=book).order_by("-created_at", "-id")[
            :ROW_LIMIT
        ]
    )
    allocations = tuple(
        SettlementAllocation.objects.filter(book=book).order_by("-created_at", "-id")[
            :ROW_LIMIT
        ]
    )
    refunds = tuple(
        Refund.objects.filter(book=book).order_by("-created_at", "-id")[:ROW_LIMIT]
    )
    payouts = tuple(
        Payout.objects.filter(book=book).order_by("-created_at", "-id")[:ROW_LIMIT]
    )
    sessions = tuple(
        CashSession.objects.filter(book=book).order_by("-created_at", "-id")[:ROW_LIMIT]
    )
    rules = tuple(
        PractitionerPayoutRule.objects.filter(book=book).order_by("-created_at", "-id")[
            :ROW_LIMIT
        ]
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.billing_practitioners(%s)", [clinic_id]
        )
        practitioners = [(str(key), label) for key, label in cursor.fetchall()]
    invoices = tuple(
        Invoice.objects.filter(
            clinic_id=clinic_id, state__in=("open", "paid")
        ).order_by("-created_at", "-id")[:ROW_LIMIT]
    )
    names = dict(practitioners)
    rule_choices = [("", "—")]
    for row in rules:
        rate = (
            f"{row.value // 100},{row.value % 100:02d}%"
            if row.kind == "percent"
            else minor_brl(row.value)
        )
        name = names.get(str(row.practitioner_id), str(row.practitioner_id)[:8])
        rule_choices.append((str(row.pk), f"{name} · {rate} · {row.policy_version}"))
    choices = {
        "invoice_ids": [
            (str(row.invoice_id), str(row.invoice.reference)[:12]) for row in terms
        ],
        "practitioner_id": practitioners,
        "invoice_id": [
            (str(row.pk), f"{str(row.reference)[:12]} · {minor_brl(row.amount_minor)}")
            for row in invoices
        ],
        "settlement_id": [
            (
                str(row.pk),
                f"{str(row.pk)[:8]} · {minor_brl(row.amount_minor)} · "
                f"{money_state(row.state)}",
            )
            for row in settlements
        ],
        "allocation_id": [
            (str(row.pk), f"{str(row.pk)[:8]} · {minor_brl(row.amount_minor)}")
            for row in allocations
        ],
        "rule_id": rule_choices,
        "refund_id": [
            (str(row.pk), f"{str(row.pk)[:8]} · {money_state(row.state)}")
            for row in refunds
        ],
        "payout_id": [
            (str(row.pk), f"{str(row.pk)[:8]} · {money_state(row.state)}")
            for row in payouts
        ],
        "session_id": [
            (
                str(row.pk),
                f"{str(row.pk)[:8]} · r{row.revision} · {money_state(row.state)}",
            )
            for row in sessions
        ],
    }
    if not can_read:
        return context, choices
    totals = account_balances(book=book)
    receivables = receivable_balances(book=book, terms=terms)
    context.update(
        {
            "clinic_timezone": book.clinic.timezone,
            "settlements": settlements,
            "refunds": refunds,
            "payouts": payouts,
            "cash_sessions": sessions,
            "accounts": sorted(totals.items()),
            "receivables": tuple(
                Receivable(
                    term.invoice.reference,
                    term.net_minor,
                    receivables[term.pk],
                    term.completion_entry_id is not None,
                )
                for term in terms
            ),
            "exceptions": tuple(
                row for row in settlements if row.state in {"observed", "quarantined"}
            ),
            "suspense_lines": suspense_items(book=book)[:ROW_LIMIT],
        }
    )
    return context, choices
