"""Todo 56: exact arithmetic and database-enforced journal invariants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from itertools import product
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.journal import (
    JournalConflictError,
    Line,
    SourceFact,
    largest_remainder,
    post_entry,
    round_reais,
)
from apps.billing.models import JournalEntry, JournalLine, Refund
from apps.billing.refunds import request_refund
from apps.tenancy.db import tenant_context
from django.db import (
    IntegrityError,
    connection,
    connections,
)
from psycopg import Error as PostgresError

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ("finance_setup",)

# The economic examples are deliberately independent of a starting balance.
POSTINGS = (
    (
        "completion",
        (("patient_ar", "debit", 10000), ("service_revenue", "credit", 10000)),
    ),
    (
        "prepayment_applied",
        (("patient_advances", "debit", 10000), ("patient_ar", "credit", 10000)),
    ),
    (
        "collection_repasse",
        (
            ("bank", "debit", 10000),
            ("patient_ar", "credit", 10000),
            ("practitioner_compensation", "debit", 3000),
            ("practitioner_payables", "credit", 3000),
        ),
    ),
    (
        "refund_reversal",
        (
            ("contra_revenue", "debit", 5000),
            ("bank", "credit", 5000),
            ("practitioner_payables", "debit", 1500),
            ("practitioner_compensation", "credit", 1500),
        ),
    ),
    ("psp_fee", (("psp_fees", "debit", 137), ("psp_clearing", "credit", 137))),
    (
        "insurer_overpayment",
        (
            ("bank", "debit", 11000),
            ("insurer_ar", "credit", 10000),
            ("insurer_overpayments", "credit", 1000),
        ),
    ),
    (
        "unidentified_receipt",
        (("bank", "debit", 10000), ("unidentified_receipts", "credit", 10000)),
    ),
)


@pytest.mark.parametrize(
    ("example", "specification"), POSTINGS, ids=[row[0] for row in POSTINGS]
)
@pytest.mark.django_db(transaction=True)
def test_seven_balanced_postings_commit_as_runtime(
    finance_setup: FinanceSetup,
    example: str,
    specification: Sequence[tuple[str, str, int]],
) -> None:
    # Given: one clinic book and the minimum chart.
    lines = tuple(
        Line(
            account,
            side,
            amount,
            practitioner_id=finance_setup.graph.physician
            if account in {"practitioner_payables", "practitioner_compensation"}
            else None,
        )
        for account, side, amount in specification
    )
    with finance_scope(finance_setup):
        # When: the complete economic example is sealed and committed.
        entry = post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=lines,
        )
        assert entry.state == "posted"
    with finance_scope(finance_setup):
        # Then: persisted sides balance, not merely the input tuple.
        stored = tuple(JournalLine.objects.filter(entry=entry))
        assert sum(row.amount_minor for row in stored if row.side == "debit") == sum(
            row.amount_minor for row in stored if row.side == "credit"
        )
        assert len(stored) == len(specification), example


@pytest.mark.django_db(transaction=True)
def test_unbalanced_entry_is_rejected_only_when_transaction_commits(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a deliberately wrong entry bypasses no roles or constraints.
    reached_after_post = False

    def commit_unbalanced() -> None:
        nonlocal reached_after_post
        with finance_scope(finance_setup):
            post_entry(
                book_id=finance_setup.book_id,
                source_fact=SourceFact("manual", uuid4()),
                lines=(
                    Line("bank", "debit", 101),
                    Line("opening_balances", "credit", 100),
                ),
            )
            reached_after_post = True

    # When: posting returns, but the enclosing durable transaction commits.
    with pytest.raises(IntegrityError) as rejected:
        commit_unbalanced()
    # Then: the deferred constraint, not an eager application check, refused it.
    assert reached_after_post
    cause = rejected.value.__cause__
    assert isinstance(cause, PostgresError)
    assert cause.sqlstate == "23514"
    assert cause.diag.constraint_name == "billing_entry_balanced"
    with finance_scope(finance_setup):
        assert not JournalEntry.objects.exists()


# New: 101 percentages x 4 amounts x 3 weights x 2 orders; not missing U1.
ALLOCATION_CASES = tuple(
    product(
        range(101),
        (1, 2, 10001, 2**63 - 1),
        ((1, 1), (1, 2, 3), (0, 1, 1)),
        (False, True),
    )
)


@pytest.mark.parametrize(
    ("percentage", "amount", "weights", "reverse"), ALLOCATION_CASES
)
def test_allocation_corpus_conserves_every_cent_and_uses_stable_remainders(
    percentage: int, amount: int, weights: tuple[int, ...], reverse: bool
) -> None:
    # Given: explicit integer dimensions include zero weights and bigint edges.
    keys = tuple(f"{index:02d}" for index in range(len(weights)))
    order = tuple(reversed(keys)) if reverse else keys
    shares = tuple((key, weights[int(key)]) for key in order)
    whole = round(Fraction(amount * percentage, 100))
    # When: largest remainder allocates once, independent of input ordering.
    parts = largest_remainder(whole, shares)
    # Then: an independent rational oracle proves conservation and tie priority.
    exact = {key: Fraction(whole * weight, sum(weights)) for key, weight in shares}
    floors = {key: value.numerator // value.denominator for key, value in exact.items()}
    residual = whole - sum(floors.values())
    priority = sorted(keys, key=lambda key: (-(exact[key] - floors[key]), key))
    expected = {key: floors[key] + (key in priority[:residual]) for key in keys}
    assert parts == expected
    assert sum(parts.values()) == whole
    assert all(parts[key] == 0 for key, weight in shares if weight == 0)


@pytest.mark.parametrize(("reais", "minor"), [("10.005", 1000), ("10.015", 1002)])
@pytest.mark.django_db(transaction=True)
def test_half_even_is_applied_at_the_versioned_reais_boundary(
    reais: str, minor: int
) -> None:
    # Given: decimal text has not been converted to float or Decimal.
    # When/Then: only the documented conversion boundary rounds.
    assert round_reais(reais) == minor
    with runtime_role(), connection.cursor() as cursor:
        numerator, denominator = (10005, 10) if minor == 1000 else (10015, 10)
        cursor.execute(
            "SELECT clinic_app.billing_round_ratio(%s, %s)", [numerator, denominator]
        )
        assert cursor.fetchone() == (minor,)


@pytest.mark.django_db(transaction=True)
def test_refund_reservations_serialize_with_a_barrier(
    finance_setup: FinanceSetup,
) -> None:
    # Given: two requests each fit alone, but together exceed verified collection.
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        allocation = allocated(finance_setup, terms, 1000)
    barrier = Barrier(2, timeout=15)

    def reserve() -> str:
        connections.close_all()
        try:
            with (
                runtime_role(),
                tenant_context(
                    finance_setup.graph.clinic_admin, finance_setup.graph.organization_a
                ),
            ):
                barrier.wait()
                request_refund(
                    clinic_id=finance_setup.clinic_id,
                    allocation_id=allocation.pk,
                    amount_minor=600,
                    command_id=uuid4(),
                )
                return "reserved"
        except JournalConflictError:
            return "refused"
        finally:
            connections.close_all()

    # When: both real runtime-role transactions cross the same cut point.
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: reserve(), range(2)))
    # Then: commit leaves exactly one reservation, never 1,200 against 1,000.
    assert sorted(results) == ["refused", "reserved"]
    with finance_scope(finance_setup):
        assert list(Refund.objects.values_list("amount_minor", flat=True)) == [600]
