"""One transfer line clears exactly one funded instruction in a book."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import JournalEntry, Payout, Refund
from apps.billing.money_types import JournalConflictError
from apps.billing.payouts import approve_payout, request_payout, transition_payout
from apps.billing.refunds import approve_refund, request_refund, transition_refund
from apps.billing.security import finance_session
from django.db import connection, connections

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from billing.test_accounting_policy import balance

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_claim_is_private_and_cannot_be_mutated_by_runtime() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity,relowner::regrole::text,"
            "has_table_privilege('clinic_app',oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'),"
            "has_table_privilege('clinic_agent',oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')"
            " FROM pg_class WHERE oid="
            "'clinic_app.billing_transferconfirmation'::regclass"
        )
        assert cursor.fetchone() == (True, True, "clinic_owner", False, False)
        cursor.execute(
            "SELECT polname FROM pg_policy WHERE polrelid="
            "'clinic_app.billing_transferconfirmation'::regclass"
        )
        assert cursor.fetchall() == []


def _instruction(setup: FinanceSetup, kind: str, allocation: UUID) -> Refund | Payout:
    if kind == "payout":
        payout = request_payout(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.graph.physician,
            amount_minor=100,
            command_id=uuid4(),
        )
        approve_payout(clinic_id=setup.clinic_id, payout_id=payout.pk)
        return transition_payout(
            clinic_id=setup.clinic_id, payout_id=payout.pk, next_state="sending"
        )
    refund = request_refund(
        clinic_id=setup.clinic_id,
        allocation_id=allocation,
        amount_minor=100,
        command_id=uuid4(),
    )
    approve_refund(clinic_id=setup.clinic_id, refund_id=refund.pk)
    return transition_refund(
        clinic_id=setup.clinic_id, refund_id=refund.pk, next_state="sending"
    )


def _confirm(
    setup: FinanceSetup, target: Refund | Payout, proof: UUID
) -> Refund | Payout:
    if isinstance(target, Payout):
        return transition_payout(
            clinic_id=setup.clinic_id,
            payout_id=target.pk,
            next_state="confirmed",
            evidence_reference=proof,
        )
    return transition_refund(
        clinic_id=setup.clinic_id,
        refund_id=target.pk,
        next_state="confirmed",
        evidence_reference=proof,
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [("refund", "refund"), ("payout", "payout"), ("refund", "payout")],
)
def test_one_transfer_line_cannot_clear_two_instructions(
    finance_setup: FinanceSetup, first: str, second: str
) -> None:
    setup = finance_setup
    terms = invoice_terms(setup)
    key = fresh_finance_session(setup)
    proof = uuid4()
    with (
        finance_scope(setup),
        finance_session(clinic_id=setup.clinic_id, session_key=key),
    ):
        allocation = allocated(setup, terms, 1000)
        targets = tuple(
            _instruction(setup, kind, allocation.pk) for kind in (first, second)
        )
    cut = Barrier(2)

    def claim(target: Refund | Payout) -> str:
        connections.close_all()
        try:
            with finance_scope(setup):
                cut.wait()
                with finance_session(clinic_id=setup.clinic_id, session_key=key):
                    return _confirm(setup, target, proof).state
        except JournalConflictError:
            return "refused"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(claim, targets))
    with (
        finance_scope(setup),
        finance_session(clinic_id=setup.clinic_id, session_key=key),
    ):
        for target in targets:
            target.refresh_from_db()
        assert sorted(target.state for target in targets) == ["confirmed", "sending"]
        assert sorted(results) == ["confirmed", "refused"]
        confirmed, held = sorted(targets, key=lambda target: target.state)
        assert _confirm(setup, confirmed, proof).pk == confirmed.pk
        assert held.evidence_reference is None
        assert balance(setup.book_id, "bank") == 900
        assert (
            balance(setup.book_id, "payout_in_transit")
            + balance(setup.book_id, "refunds_payable")
            == -100
        )
        postings = JournalEntry.objects.filter(
            source_kind__in=("refund_confirmed", "payout_confirmed")
        )
        assert list(postings.values_list("source_id", flat=True)) == [confirmed.pk]
        # A distinct transfer line on the same bank statement is legitimate.
        assert _confirm(setup, held, uuid4()).state == "confirmed"
        assert postings.count() == 2
        assert balance(setup.book_id, "bank") == 800
