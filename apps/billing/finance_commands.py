"""Translate validated native forms to the clinic-scoped money services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from apps.billing.cash import close_cash, open_cash, reopen_cash
from apps.billing.collections import (
    allocate_proportionally,
    allocate_settlement,
    observe_settlement,
    post_settlement,
    quarantine_settlement,
    verify_settlement,
)
from apps.billing.compensation import (
    create_payout_rule,
    prepare_invoice_terms,
    record_completion,
)
from apps.billing.journal import Line, SourceFact, create_book, post_entry
from apps.billing.money_types import (
    ChargeAdjustments,
    PayoutRuleTerms,
    SettlementObservation,
)
from apps.billing.opening import record_opening_balance
from apps.billing.payouts import approve_payout, request_payout, transition_payout
from apps.billing.refunds import (
    approve_refund,
    request_refund,
    request_unallocated_refund,
    transition_refund,
)
from apps.billing.security import scoped_book

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Model

    from apps.billing.finance_forms import FinanceForm
    from apps.billing.models import JournalEntry, SettlementAllocation


def _record_fee(clinic_id: UUID, form: FinanceForm) -> JournalEntry:
    book = scoped_book(clinic_id=clinic_id, permission="settlement.post")
    data = form.cleaned_data
    return post_entry(
        book_id=book.pk,
        source_fact=SourceFact("manual", data["command_id"]),
        lines=(
            Line("psp_fees", "debit", data["amount"]),
            Line(data["account"], "credit", data["amount"]),
        ),
    )


HANDLERS: Final[
    dict[str, Callable[[UUID, FinanceForm], Model | tuple[SettlementAllocation, ...]]]
] = {
    "create_book": lambda clinic, _form: create_book(clinic_id=clinic),
    "opening_balance": lambda clinic, form: record_opening_balance(
        clinic_id=clinic,
        account=form.cleaned_data["account"],
        amount_minor=form.cleaned_data["amount"],
        evidence_reference=form.cleaned_data["evidence_reference"],
    ),
    "create_rule": lambda clinic, form: create_payout_rule(
        clinic_id=clinic,
        practitioner_id=UUID(form.cleaned_data["practitioner_id"]),
        terms=PayoutRuleTerms(
            form.cleaned_data["kind"], form.cleaned_data["rule_value"]
        ),
        command_id=form.cleaned_data["command_id"],
    ),
    "prepare_terms": lambda clinic, form: prepare_invoice_terms(
        clinic_id=clinic,
        invoice_id=UUID(form.cleaned_data["invoice_id"]),
        payer=form.cleaned_data["payer"],
        adjustments=ChargeAdjustments(
            form.cleaned_data["discount"], form.cleaned_data["adjustment"]
        ),
        payout_rule_id=UUID(form.cleaned_data["rule_id"])
        if form.cleaned_data["rule_id"]
        else None,
    ),
    "complete_service": lambda clinic, form: record_completion(
        clinic_id=clinic,
        invoice_id=UUID(form.cleaned_data["invoice_id"]),
        evidence_reference=form.cleaned_data["evidence_reference"],
    ),
    "observe": lambda clinic, form: observe_settlement(
        clinic_id=clinic,
        observation=SettlementObservation(
            form.cleaned_data["amount"],
            form.cleaned_data["receipt_kind"],
            form.cleaned_data["account"],
            form.cleaned_data["evidence_reference"],
        ),
    ),
    "verify": lambda clinic, form: verify_settlement(
        clinic_id=clinic, settlement_id=UUID(form.cleaned_data["settlement_id"])
    ),
    "post": lambda clinic, form: post_settlement(
        clinic_id=clinic, settlement_id=UUID(form.cleaned_data["settlement_id"])
    ),
    "quarantine": lambda clinic, form: quarantine_settlement(
        clinic_id=clinic, settlement_id=UUID(form.cleaned_data["settlement_id"])
    ),
    "allocate": lambda clinic, form: allocate_settlement(
        clinic_id=clinic,
        settlement_id=UUID(form.cleaned_data["settlement_id"]),
        invoice_id=UUID(form.cleaned_data["invoice_id"]),
        amount_minor=form.cleaned_data["amount"],
        command_id=form.cleaned_data["command_id"],
    ),
    "allocate_proportionally": lambda clinic, form: allocate_proportionally(
        clinic_id=clinic,
        settlement_id=UUID(form.cleaned_data["settlement_id"]),
        invoice_ids=tuple(UUID(value) for value in form.cleaned_data["invoice_ids"]),
        command_id=form.cleaned_data["command_id"],
    ),
    "request_refund": lambda clinic, form: request_refund(
        clinic_id=clinic,
        allocation_id=UUID(form.cleaned_data["allocation_id"]),
        amount_minor=form.cleaned_data["amount"],
        command_id=form.cleaned_data["command_id"],
        kind=form.cleaned_data["refund_kind"],
    ),
    "request_unallocated_refund": lambda clinic, form: request_unallocated_refund(
        clinic_id=clinic,
        settlement_id=UUID(form.cleaned_data["settlement_id"]),
        amount_minor=form.cleaned_data["amount"],
        command_id=form.cleaned_data["command_id"],
    ),
    "request_payout": lambda clinic, form: request_payout(
        clinic_id=clinic,
        practitioner_id=UUID(form.cleaned_data["practitioner_id"]),
        amount_minor=form.cleaned_data["amount"],
        command_id=form.cleaned_data["command_id"],
    ),
    "approve_refund": lambda clinic, form: approve_refund(
        clinic_id=clinic, refund_id=UUID(form.cleaned_data["refund_id"])
    ),
    "approve_payout": lambda clinic, form: approve_payout(
        clinic_id=clinic, payout_id=UUID(form.cleaned_data["payout_id"])
    ),
    "refund_state": lambda clinic, form: transition_refund(
        clinic_id=clinic,
        refund_id=UUID(form.cleaned_data["refund_id"]),
        next_state=form.cleaned_data["outcome"],
        evidence_reference=form.cleaned_data["outcome_evidence"],
    ),
    "payout_state": lambda clinic, form: transition_payout(
        clinic_id=clinic,
        payout_id=UUID(form.cleaned_data["payout_id"]),
        next_state=form.cleaned_data["outcome"],
        evidence_reference=form.cleaned_data["outcome_evidence"],
    ),
    "open_cash": lambda clinic, form: open_cash(
        clinic_id=clinic,
        counted_minor=form.cleaned_data["counted"],
        command_id=form.cleaned_data["command_id"],
    ),
    "close_cash": lambda clinic, form: close_cash(
        clinic_id=clinic,
        session_id=UUID(form.cleaned_data["session_id"]),
        counted_minor=form.cleaned_data["counted"],
        expected_revision=form.cleaned_data["expected_revision"],
        command_id=form.cleaned_data["command_id"],
    ),
    "reopen_cash": lambda clinic, form: reopen_cash(
        clinic_id=clinic,
        session_id=UUID(form.cleaned_data["session_id"]),
        reason=form.cleaned_data["reason"],
        expected_revision=form.cleaned_data["expected_revision"],
        command_id=form.cleaned_data["command_id"],
    ),
    "psp_fee": _record_fee,
}


def execute_command(*, clinic_id: UUID, form: FinanceForm) -> None:
    """Dispatch one validated action through the finite handler registry."""
    HANDLERS[form.action](clinic_id, form)
