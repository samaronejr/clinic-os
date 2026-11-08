"""A zero net suspense account does not explain different owners' open items."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from apps.billing.cash import close_cash, open_cash
from apps.billing.journal import JournalConflictError, Line, SourceFact, post_entry

from billing.journal_support import FinanceSetup, finance_scope, finance_setup

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_suspense_requires_a_real_scoped_owner_not_an_arbitrary_uuid(
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup), pytest.raises(JournalConflictError):
        post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(
                Line(
                    "suspense", "debit", 100, owner_id=uuid4(), due_on=date(2035, 1, 1)
                ),
                Line("bank", "credit", 100),
            ),
        )


def test_cash_close_does_not_net_unresolved_suspense_between_owners(
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup):
        session = open_cash(
            clinic_id=finance_setup.clinic_id, counted_minor=0, command_id=uuid4()
        )
        post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(
                Line(
                    "suspense",
                    "debit",
                    100,
                    owner_id=finance_setup.graph.clinic_admin,
                    due_on=date(2035, 1, 1),
                ),
                Line(
                    "suspense",
                    "credit",
                    100,
                    owner_id=finance_setup.graph.shared_user,
                    due_on=date(2035, 1, 1),
                ),
            ),
        )
        with pytest.raises(JournalConflictError):
            close_cash(
                clinic_id=finance_setup.clinic_id,
                session_id=session.pk,
                counted_minor=0,
                expected_revision=session.revision,
                command_id=uuid4(),
            )
