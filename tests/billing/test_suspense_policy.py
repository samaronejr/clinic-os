"""A zero net suspense account does not explain different owners' open items."""

from __future__ import annotations

import json
from datetime import date
from typing import cast
from uuid import uuid4

import pytest
from apps.billing.cash import close_cash, open_cash
from apps.billing.journal import JournalConflictError, Line, SourceFact, post_entry
from apps.billing.money_types import JournalInputError
from apps.billing.security import book_write
from django.db import DataError, connection, transaction

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


@pytest.mark.parametrize("text", ["now", "today"])
def test_due_on_clock_text_is_refused_in_python_and_in_sql(
    finance_setup: FinanceSetup, text: str
) -> None:
    # Given: a suspense line whose due date is clock text instead of a date.
    owner = uuid4()
    lines = (
        Line("suspense", "debit", 100, owner_id=owner, due_on=cast("date", text)),
        Line("bank", "credit", 100),
    )
    # When/Then: the Python boundary refuses it before any SQL runs.
    with finance_scope(finance_setup), pytest.raises(JournalInputError):
        post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=lines,
        )
    # And: SQL never parses line text, so the same JSON without a typed date fails.
    payload = [
        {
            "account": "suspense",
            "side": "debit",
            "amount_minor": "100",
            "practitioner_id": None,
            "owner_id": str(owner),
            "due_on": text,
        },
        {
            "account": "bank",
            "side": "credit",
            "amount_minor": "100",
            "practitioner_id": None,
            "owner_id": None,
            "due_on": None,
        },
    ]
    with (
        finance_scope(finance_setup),
        book_write(clinic_id=finance_setup.clinic_id, permission="settlement.post"),
        pytest.raises(DataError, match="invalid journal line"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT * FROM clinic_app.billing_post(%s,%s,%s,%s::jsonb,%s)",
            [finance_setup.book_id, "manual", uuid4(), json.dumps(payload), None],
        )
