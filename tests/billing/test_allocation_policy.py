"""Largest remainder is exercised through real many-to-many allocation writes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.billing.collections import allocate_proportionally
from apps.billing.compensation import prepare_invoice_terms
from apps.billing.journal import JournalConflictError
from apps.billing.services import create_invoice, issue_invoice

from billing.journal_support import (
    FinanceSetup,
    collected,
    finance_scope,
    finance_setup,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_one_receipt_splits_across_invoices_and_replays_its_original_partition(
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup):
        # Given: the exact outstanding weights are 101 and 202.
        invoices = []
        for amount in (101, 202):
            invoice = create_invoice(
                clinic_id=finance_setup.clinic_id,
                patient_id=finance_setup.appointment.patient_id,
                amount_minor=amount,
                idempotency_key=uuid4(),
            )
            issue_invoice(
                clinic_id=finance_setup.clinic_id,
                invoice_id=invoice.pk,
                expected_revision=invoice.revision,
            )
            prepare_invoice_terms(
                clinic_id=finance_setup.clinic_id, invoice_id=invoice.pk
            )
            invoices.append(invoice)
        settlement = collected(finance_setup, 101)
        command = uuid4()
        # When: one verified receipt is proportionally allocated to both charges.
        rows = allocate_proportionally(
            clinic_id=finance_setup.clinic_id,
            settlement_id=settlement.pk,
            invoice_ids=tuple(row.pk for row in invoices),
            command_id=command,
        )
        # Then: the highest fractional remainder receives the last cent.
        assert {row.terms.invoice_id: row.amount_minor for row in rows} == {
            invoices[0].pk: 34,
            invoices[1].pk: 67,
        }
        repeated = allocate_proportionally(
            clinic_id=finance_setup.clinic_id,
            settlement_id=settlement.pk,
            invoice_ids=tuple(row.pk for row in reversed(invoices)),
            command_id=command,
        )
        assert {row.pk for row in repeated} == {row.pk for row in rows}
        with pytest.raises(JournalConflictError):
            allocate_proportionally(
                clinic_id=finance_setup.clinic_id,
                settlement_id=settlement.pk,
                invoice_ids=(invoices[0].pk,),
                command_id=command,
            )
        # PostgreSQL SUM(bigint) totals re-enter as exact integers.
        remaining = collected(finance_setup, 202)
        final = allocate_proportionally(
            clinic_id=finance_setup.clinic_id,
            settlement_id=remaining.pk,
            invoice_ids=tuple(row.pk for row in invoices),
            command_id=uuid4(),
        )
        assert {row.terms.invoice_id: row.amount_minor for row in final} == {
            invoices[0].pk: 67,
            invoices[1].pk: 135,
        }
