"""Refunded advances can be collected again without inventing practitioner cents."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.compensation import (
    create_payout_rule,
    prepare_invoice_terms,
    record_completion,
)
from apps.billing.money_types import PayoutRuleTerms
from apps.billing.refunds import approve_refund, request_refund, transition_refund
from apps.billing.security import finance_session
from apps.billing.services import create_invoice, issue_invoice
from apps.tenancy.db import tenant_context

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    fresh_finance_session,
)
from billing.test_accounting_policy import balance
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.billing.models import InvoiceTerms, SettlementAllocation


pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def fixed_terms(setup: FinanceSetup, net: int, fee: int) -> InvoiceTerms:
    with (
        runtime_role(),
        tenant_context(setup.graph.shared_user, setup.graph.organization_a),
    ):
        rule = create_payout_rule(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.graph.physician,
            terms=PayoutRuleTerms("fixed", fee),
            command_id=uuid4(),
        )
    with finance_scope(setup):
        invoice = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.appointment.patient_id,
            amount_minor=net,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        return prepare_invoice_terms(
            clinic_id=setup.clinic_id, invoice_id=invoice.pk, payout_rule_id=rule.pk
        )


def confirmed_return(
    setup: FinanceSetup, allocation: SettlementAllocation, amount: int, key: str
) -> None:
    refund = request_refund(
        clinic_id=setup.clinic_id,
        allocation_id=allocation.pk,
        amount_minor=amount,
        command_id=uuid4(),
    )
    with finance_session(clinic_id=setup.clinic_id, session_key=key):
        approve_refund(clinic_id=setup.clinic_id, refund_id=refund.pk)
        transition_refund(
            clinic_id=setup.clinic_id, refund_id=refund.pk, next_state="sending"
        )
        transition_refund(
            clinic_id=setup.clinic_id,
            refund_id=refund.pk,
            next_state="confirmed",
            evidence_reference=uuid4(),
        )


def test_refunded_prepayment_can_settle_the_completion_receivable_again(
    finance_setup: FinanceSetup,
) -> None:
    # Half of two prepaid cents is returned; half-even reverses zero repasse.
    terms = fixed_terms(finance_setup, 2, 1)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        original = allocated(finance_setup, terms, 2)
        confirmed_return(finance_setup, original, 1, key)
        record_completion(
            clinic_id=finance_setup.clinic_id,
            invoice_id=terms.invoice_id,
            evidence_reference=uuid4(),
        )
        assert balance(finance_setup.book_id, "patient_ar") == 1
        # When: the actual outstanding cent is independently received again.
        replacement = allocated(finance_setup, terms, 1)
        # Then: neither historical gross capacity nor a rounding cent blocks settlement.
        assert replacement.repasse_minor == 0
        assert balance(finance_setup.book_id, "patient_ar") == 0
        assert balance(finance_setup.book_id, "practitioner_payables") == -1


def test_signed_rounding_correction_and_its_refund_keep_full_lifecycle_exact(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a one-cent fixed fee accrues on the third of five two-cent installments.
    terms = fixed_terms(finance_setup, 10, 1)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        originals = tuple(allocated(finance_setup, terms, 2) for _ in range(5))
        assert [row.repasse_minor for row in originals] == [0, 0, 1, 0, 0]
        for index in (0, 1, 3, 4):
            confirmed_return(finance_setup, originals[index], 2, key)
        record_completion(
            clinic_id=finance_setup.clinic_id,
            invoice_id=terms.invoice_id,
            evidence_reference=uuid4(),
        )
        # Net collection 2 -> 3 makes the fixed half-even entitlement zero.
        correction = allocated(finance_setup, terms, 1)
        # Keep the old rounding cent as a linked debit correction.
        assert (correction.repasse_minor, correction.repasse_side) == (1, "debit")
        assert correction.entry is not None
        assert correction.entry.reversal_of_id == originals[2].entry_id
        assert balance(finance_setup.book_id, "practitioner_payables") == 0
        final = allocated(finance_setup, terms, 7)
        assert balance(finance_setup.book_id, "patient_ar") == 0
        assert balance(finance_setup.book_id, "practitioner_payables") == -1
        # Returning every remaining allocation also reverses the signed correction.
        for row in (originals[2], correction, final):
            confirmed_return(finance_setup, row, row.amount_minor, key)
        assert balance(finance_setup.book_id, "practitioner_payables") == 0
        assert balance(finance_setup.book_id, "practitioner_compensation") == 0
        assert balance(finance_setup.book_id, "bank") == 0
