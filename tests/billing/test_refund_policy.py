"""Refund and chargeback reservation, cumulative rounding and liability reversal."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.billing.collections import allocate_settlement
from apps.billing.journal import JournalConflictError
from apps.billing.models import JournalEntry
from apps.billing.refunds import (
    approve_refund,
    request_refund,
    request_unallocated_refund,
    transition_refund,
)
from apps.billing.security import finance_session

from billing.journal_support import (
    FinanceSetup,
    allocated,
    collected,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from billing.test_accounting_policy import balance

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


@pytest.mark.parametrize("kind", ["refund", "chargeback"])
def test_split_refunds_reverse_exact_total_entitlement_with_cumulative_half_even(
    finance_setup: FinanceSetup, kind: str
) -> None:
    terms = invoice_terms(finance_setup, amount_minor=101)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        allocation = allocated(finance_setup, terms, 101)
        for amount in (33, 33, 35):
            refund = request_refund(
                clinic_id=finance_setup.clinic_id,
                allocation_id=allocation.pk,
                amount_minor=amount,
                command_id=uuid4(),
                kind=kind,
            )
            with finance_session(clinic_id=finance_setup.clinic_id, session_key=key):
                approve_refund(clinic_id=finance_setup.clinic_id, refund_id=refund.pk)
                transition_refund(
                    clinic_id=finance_setup.clinic_id,
                    refund_id=refund.pk,
                    next_state="sending",
                )
                transition_refund(
                    clinic_id=finance_setup.clinic_id,
                    refund_id=refund.pk,
                    next_state="confirmed",
                    evidence_reference=uuid4(),
                )
        assert balance(finance_setup.book_id, "practitioner_payables") == 0
        assert balance(finance_setup.book_id, "practitioner_compensation") == 0
        assert allocation.repasse_minor == 30


def test_unallocated_insurer_overpayment_refund_cannot_be_allocated_while_reserved(
    finance_setup: FinanceSetup,
) -> None:
    terms = invoice_terms(finance_setup, payer="insurer")
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        settlement = collected(finance_setup, 11000, kind="insurer")
        allocate_settlement(
            clinic_id=finance_setup.clinic_id,
            settlement_id=settlement.pk,
            invoice_id=terms.invoice_id,
            amount_minor=10000,
            command_id=uuid4(),
        )
        refund = request_unallocated_refund(
            clinic_id=finance_setup.clinic_id,
            settlement_id=settlement.pk,
            amount_minor=1000,
            command_id=uuid4(),
        )
        with pytest.raises(JournalConflictError):
            request_unallocated_refund(
                clinic_id=finance_setup.clinic_id,
                settlement_id=settlement.pk,
                amount_minor=1,
                command_id=uuid4(),
            )
        with finance_session(clinic_id=finance_setup.clinic_id, session_key=key):
            approve_refund(clinic_id=finance_setup.clinic_id, refund_id=refund.pk)
            transition_refund(
                clinic_id=finance_setup.clinic_id,
                refund_id=refund.pk,
                next_state="sending",
            )
            transition_refund(
                clinic_id=finance_setup.clinic_id,
                refund_id=refund.pk,
                next_state="confirmed",
                evidence_reference=uuid4(),
            )
        assert balance(finance_setup.book_id, "insurer_overpayments") == 0
        assert balance(finance_setup.book_id, "practitioner_payables") == -3000
        assert not JournalEntry.objects.filter(
            source_kind="repasse_reversal", source_id=refund.pk
        ).exists()
