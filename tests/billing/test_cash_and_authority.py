"""Finance separation, persisted step-up, cash evidence and payout reservations."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.billing.cash import close_cash, open_cash, reopen_cash
from apps.billing.journal import JournalConflictError
from apps.billing.models import CashSessionEvent, JournalEntry
from apps.billing.opening import record_opening_balance
from apps.billing.payouts import approve_payout, request_payout, transition_payout
from apps.billing.refunds import approve_refund, request_refund
from apps.billing.security import finance_session
from apps.billing.services import BillingAccessDeniedError
from apps.identity.stepup import StepUpRequired
from apps.tenancy.db import tenant_context

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from patient_service_support import runtime_role

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_cash_close_has_zero_unexplained_balance_and_reopen_requires_persisted_step_up(
    finance_setup: FinanceSetup,
) -> None:
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        # Given: no petty-cash movements and an observed zero opening balance.
        session = open_cash(
            clinic_id=finance_setup.clinic_id, counted_minor=0, command_id=uuid4()
        )
        # When: the counted close agrees with the sealed ledger.
        closed = close_cash(
            clinic_id=finance_setup.clinic_id,
            session_id=session.pk,
            counted_minor=0,
            expected_revision=session.revision,
            command_id=uuid4(),
        )
        # Then: the close retains its count, and a timestamp/role alone cannot reopen.
        assert closed.state == "closed"
        with pytest.raises(StepUpRequired):
            reopen_cash(
                clinic_id=finance_setup.clinic_id,
                session_id=session.pk,
                reason="correction",
                expected_revision=closed.revision,
                command_id=uuid4(),
            )
        with finance_session(clinic_id=finance_setup.clinic_id, session_key=key):
            reopened = reopen_cash(
                clinic_id=finance_setup.clinic_id,
                session_id=session.pk,
                reason="correction",
                expected_revision=closed.revision,
                command_id=uuid4(),
            )
        assert reopened.state == "open"
        assert list(
            CashSessionEvent.objects.filter(session=session)
            .order_by("revision")
            .values_list("state", flat=True)
        ) == ["opened", "closed", "reopened"]


def test_first_cash_count_requires_an_explicit_retained_opening_fact(
    finance_setup: FinanceSetup,
) -> None:
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_a
        ),
    ):
        evidence = uuid4()
        record_opening_balance(
            clinic_id=finance_setup.clinic_id,
            account="petty_cash",
            amount_minor=10000,
            evidence_reference=evidence,
        )
        repeated = record_opening_balance(
            clinic_id=finance_setup.clinic_id,
            account="petty_cash",
            amount_minor=10000,
            evidence_reference=evidence,
        )
        assert repeated.source_id == evidence
    with finance_scope(finance_setup):
        session = open_cash(
            clinic_id=finance_setup.clinic_id, counted_minor=10000, command_id=uuid4()
        )
        assert session.expected_minor == 10000


def test_finance_approval_does_not_inherit_legacy_manager_equivalence(
    finance_setup: FinanceSetup,
) -> None:
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        allocation = allocated(finance_setup, terms, 1000)
        refund = request_refund(
            clinic_id=finance_setup.clinic_id,
            allocation_id=allocation.pk,
            amount_minor=100,
            command_id=uuid4(),
        )
        with pytest.raises(StepUpRequired):
            approve_refund(clinic_id=finance_setup.clinic_id, refund_id=refund.pk)
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_a
        ),
        pytest.raises(BillingAccessDeniedError),
    ):
        approve_refund(clinic_id=finance_setup.clinic_id, refund_id=refund.pk)


def test_payout_reserves_only_earned_payable_and_unknown_stays_in_transit(
    finance_setup: FinanceSetup,
) -> None:
    terms = invoice_terms(finance_setup)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        allocated(finance_setup, terms, 1000)
        payout = request_payout(
            clinic_id=finance_setup.clinic_id,
            practitioner_id=finance_setup.graph.physician,
            amount_minor=300,
            command_id=uuid4(),
        )
        with pytest.raises(JournalConflictError):
            request_payout(
                clinic_id=finance_setup.clinic_id,
                practitioner_id=finance_setup.graph.physician,
                amount_minor=1,
                command_id=uuid4(),
            )
        with finance_session(clinic_id=finance_setup.clinic_id, session_key=key):
            approve_payout(clinic_id=finance_setup.clinic_id, payout_id=payout.pk)
            transition_payout(
                clinic_id=finance_setup.clinic_id,
                payout_id=payout.pk,
                next_state="sending",
            )
            transition_payout(
                clinic_id=finance_setup.clinic_id,
                payout_id=payout.pk,
                next_state="unknown",
            )
        payout.refresh_from_db()
        assert payout.state == "unknown"
        assert (
            JournalEntry.objects.filter(
                book_id=finance_setup.book_id,
                source_kind="payout_approved",
                source_id=payout.pk,
            ).count()
            == 1
        )
        assert not JournalEntry.objects.filter(
            book_id=finance_setup.book_id,
            source_kind="payout_confirmed",
            source_id=payout.pk,
        ).exists()
