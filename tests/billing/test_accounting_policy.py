"""Economic policies chosen by root best judgment, not missing-U1 reconstruction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.collections import allocate_settlement
from apps.billing.compensation import record_completion
from apps.billing.journal import JournalConflictError, Line, SourceFact, post_entry
from apps.billing.models import JournalEntry, JournalLine, SettlementAllocation
from apps.billing.money_types import ChargeAdjustments
from apps.billing.refunds import approve_refund, request_refund, transition_refund
from apps.billing.security import finance_session
from apps.scheduling.services import complete, start
from apps.tenancy.db import tenant_context
from django.db.models import Sum

from billing.journal_support import (
    FinanceSetup,
    allocated,
    collected,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def balance(book_id: UUID, code: str) -> int:
    rows = JournalLine.objects.filter(entry__book_id=book_id, account__code=code)
    debit = rows.filter(side="debit").aggregate(total=Sum("amount_minor"))["total"] or 0
    credit = (
        rows.filter(side="credit").aggregate(total=Sum("amount_minor"))["total"] or 0
    )
    return int(debit) - int(credit)


def test_charge_issue_posts_nothing_and_completion_recognizes_accrual(
    finance_setup: FinanceSetup,
) -> None:
    # Given: an issued charge is not evidence that care has been delivered.
    terms = invoice_terms(finance_setup, completed=False)
    with finance_scope(finance_setup):
        assert not JournalEntry.objects.exists()
        with pytest.raises(JournalConflictError):
            record_completion(
                clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id
            )
    # When: the actual clinician completes the appointment.
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.physician, finance_setup.graph.organization_a
        ),
    ):
        appointment = terms.invoice.appointment
        assert appointment is not None
        running = start(
            clinic_id=finance_setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=appointment.revision,
            command_id=uuid4(),
        )
        complete(
            clinic_id=finance_setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=running.revision,
            command_id=uuid4(),
        )
    # Then: revenue and A/R accrue without collection or practitioner entitlement.
    with finance_scope(finance_setup):
        assert balance(finance_setup.book_id, "service_revenue") == -10000
        assert balance(finance_setup.book_id, "patient_ar") == 10000
        assert balance(finance_setup.book_id, "practitioner_payables") == 0


def test_prepaid_net_collection_accrues_repasse_before_fees_and_applies_on_completion(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a 100 reais service has a 10 discount and 5 payer adjustment.
    terms = invoice_terms(
        finance_setup, adjustments=ChargeAdjustments(1000, 500), completed=False
    )
    with finance_scope(finance_setup):
        # When: 85 reais are verified/allocated, then the clinic pays a PSP fee.
        allocation = allocated(finance_setup, terms, 8500)
        post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(Line("psp_fees", "debit", 200), Line("bank", "credit", 200)),
        )
        # Then: net collection, not gross invoice or post-fee cash, earns 30%.
        assert allocation.repasse_minor == 2550
        assert balance(finance_setup.book_id, "patient_advances") == -8500
        assert balance(finance_setup.book_id, "service_revenue") == 0
        assert balance(finance_setup.book_id, "practitioner_payables") == -2550
        assert balance(finance_setup.book_id, "psp_fees") == 200
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.physician, finance_setup.graph.organization_a
        ),
    ):
        appointment = terms.invoice.appointment
        assert appointment is not None
        running = start(
            clinic_id=finance_setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=appointment.revision,
            command_id=uuid4(),
        )
        complete(
            clinic_id=finance_setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=running.revision,
            command_id=uuid4(),
        )
    with finance_scope(finance_setup):
        assert balance(finance_setup.book_id, "patient_advances") == 0
        assert balance(finance_setup.book_id, "patient_ar") == 0
        assert balance(finance_setup.book_id, "service_revenue") == -10000
        assert balance(finance_setup.book_id, "discounts") == 1000
        assert balance(finance_setup.book_id, "payer_adjustments") == 500


def test_partial_refund_reverses_entitlement_proportionally_with_lineage(
    finance_setup: FinanceSetup,
) -> None:
    terms = invoice_terms(finance_setup)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        allocation = allocated(finance_setup, terms, 10000)
        refund = request_refund(
            clinic_id=finance_setup.clinic_id,
            allocation_id=allocation.pk,
            amount_minor=2500,
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
        assert balance(finance_setup.book_id, "practitioner_payables") == -2250
        assert balance(finance_setup.book_id, "practitioner_compensation") == 2250
        refund.refresh_from_db()
        assert refund.repasse_entry is not None
        assert refund.repasse_entry.reversal_of_id == allocation.entry_id
        assert refund.repasse_reversed_minor == 750
        assert balance(finance_setup.book_id, "refunds_payable") == 0


def test_many_to_many_allocations_and_replay_do_not_repost_facts(
    finance_setup: FinanceSetup,
) -> None:
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        first = collected(finance_setup, 6000)
        command = uuid4()
        allocation = allocate_settlement(
            clinic_id=finance_setup.clinic_id,
            settlement_id=first.pk,
            invoice_id=terms.invoice_id,
            amount_minor=6000,
            command_id=command,
        )
        count = JournalEntry.objects.count()
        repeated = allocate_settlement(
            clinic_id=finance_setup.clinic_id,
            settlement_id=first.pk,
            invoice_id=terms.invoice_id,
            amount_minor=6000,
            command_id=command,
        )
        assert allocation.pk == repeated.pk
        assert JournalEntry.objects.count() == count
        allocated(finance_setup, terms, 4000)
        assert SettlementAllocation.objects.filter(terms=terms).count() == 2
        assert balance(finance_setup.book_id, "patient_ar") == 0


def test_source_fact_replay_is_exact_and_different_content_is_refused(
    finance_setup: FinanceSetup,
) -> None:
    fact = SourceFact("manual", uuid4())
    lines = (Line("bank", "debit", 100), Line("opening_balances", "credit", 100))
    with finance_scope(finance_setup):
        first = post_entry(book_id=finance_setup.book_id, source_fact=fact, lines=lines)
        replay = post_entry(
            book_id=finance_setup.book_id,
            source_fact=fact,
            lines=tuple(reversed(lines)),
        )
        assert replay.pk == first.pk
        with pytest.raises(JournalConflictError):
            post_entry(
                book_id=finance_setup.book_id,
                source_fact=fact,
                lines=(
                    Line("bank", "debit", 101),
                    Line("opening_balances", "credit", 101),
                ),
            )
