"""Journal SQL rejects inactive actors without Python or tenant-entry admission."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.billing.models import (
    CashSession,
    CashSessionEvent,
    Invoice,
    JournalSettlement,
    Payout,
    PractitionerPayoutRule,
    Refund,
    SettlementAllocation,
)
from apps.billing.services import confirm_settlement, create_invoice, issue_invoice
from apps.identity.models import User
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from billing.journal_support import (
    FinanceSetup,
    allocated,
    collected,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)

WRITE_GUARDS = {
    "allocation": "billing_allocation_guard",
    "cash": "billing_cash_guard",
    "payout": "billing_payout_guard",
    "refund": "billing_refund_guard",
    "settlement": "billing_settlement_journal_guard",
}


def _active(setup: FinanceSetup, actor: UUID, *, active: bool) -> None:
    with owner_context(setup.graph.organization_a):
        assert User.objects.filter(pk=actor).update(is_active=active) == 1


def _sql_denial(error: DatabaseError, function: str, state: str = "42501") -> None:
    cause = error.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.sqlstate == state
    # A Python gate, EXECUTE ACL or RLS-only refusal cannot satisfy this oracle.
    assert f"{function}(" in (cause.diag.context or "")


@pytest.mark.parametrize("subject", ["actor", "practitioner"])
def test_definition_guard_rechecks_active_actor_and_practitioner(
    finance_setup: FinanceSetup, subject: str
) -> None:
    setup, graph = finance_setup, finance_setup.graph
    actor = graph.shared_user
    target = actor if subject == "actor" else graph.physician
    values = {
        "id": uuid4(),
        "organization_id": graph.organization_a,
        "book_id": setup.book_id,
        "practitioner_id": graph.physician,
        "kind": "percent",
        "value": 3000,
        "command_id": uuid4(),
        "created_at": timezone.now(),
    }
    decisions = []
    # The actor check delegates through lock_book/has_permission; the lexical
    # is_active clause in definition_guard itself checks the practitioner.
    for active in (True, False, True):
        _active(setup, target, active=active)
        with permission_context(graph, actor):
            if active:
                rule = PractitionerPayoutRule.objects.create(**values)
                assert PractitionerPayoutRule.objects.filter(pk=rule.pk).exists()
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    PractitionerPayoutRule.objects.create(**values)
                _sql_denial(
                    denied.value,
                    "billing_definition_guard",
                    "42501" if subject == "actor" else "23514",
                )
                if subject == "practitioner":
                    cause = denied.value.__cause__
                    assert isinstance(cause, psycopg.Error)
                    assert cause.diag.message_primary == "invalid practitioner policy"
                decisions.append(False)
            # Keep the exact same INSERT, command and financial state for every
            # phase. Only the persisted user flag changes between attempts.
            transaction.set_rollback(True)
    assert decisions == [True, False, True]


@pytest.mark.parametrize("kind", WRITE_GUARDS)
def test_journal_native_writes_require_current_active_actor(
    finance_setup: FinanceSetup, kind: str
) -> None:
    setup, graph = finance_setup, finance_setup.graph
    actor = graph.clinic_admin
    values: dict[str, object] = {
        "id": uuid4(),
        "organization_id": graph.organization_a,
        "book_id": setup.book_id,
        "command_id": uuid4(),
        "created_at": timezone.now(),
    }
    model: type[
        CashSession | JournalSettlement | Payout | Refund | SettlementAllocation
    ]
    if kind in {"allocation", "payout", "refund"}:
        terms = invoice_terms(setup)
        with finance_scope(setup):
            if kind == "allocation":
                settlement = collected(setup, 1000)
                model = SettlementAllocation
                values.update(
                    settlement_id=settlement.pk, terms_id=terms.pk, amount_minor=1000
                )
            else:
                allocation = allocated(setup, terms, 1000)
                values.update(amount_minor=100, requested_by_id=actor)
                if kind == "payout":
                    model = Payout
                    values.update(practitioner_id=graph.physician)
                else:
                    model = Refund
                    values.update(
                        allocation_id=allocation.pk,
                        settlement_id=allocation.settlement_id,
                    )
    elif kind == "cash":
        model = CashSession
        values.update(counted_minor=0, last_command_id=values["command_id"])
    else:
        assert kind == "settlement"
        model = JournalSettlement
        values.update(
            amount_minor=1000,
            kind="patient",
            account="bank",
            evidence_reference=uuid4(),
        )
    decisions = []
    for active in (True, False, True):
        _active(setup, actor, active=active)
        with permission_context(graph, actor):
            if active:
                row = model.objects.create(**values)
                assert model.objects.filter(pk=row.pk).exists()
                if kind == "cash":
                    event = CashSessionEvent.objects.get(session_id=row.pk)
                    assert (event.actor_id, event.state) == (actor, "opened")
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    model.objects.create(**values)
                _sql_denial(denied.value, WRITE_GUARDS[kind])
                decisions.append(False)
            transaction.set_rollback(True)
    assert decisions == [True, False, True]


@pytest.mark.parametrize("kind", ["appointment", "recognition", "legacy"])
def test_journal_resolvers_require_current_active_actor(
    finance_setup: FinanceSetup, kind: str
) -> None:
    setup, graph = finance_setup, finance_setup.graph
    actor = graph.clinic_admin
    if kind == "legacy":
        with finance_scope(setup):
            invoice = create_invoice(
                clinic_id=setup.clinic_id,
                patient_id=setup.appointment.patient_id,
                amount_minor=1000,
                idempotency_key=uuid4(),
            )
            issue_invoice(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                expected_revision=invoice.revision,
            )
            receipt = confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=uuid4(),
                amount_minor=1000,
            )
            journal = JournalSettlement.objects.get(
                legacy_settlement_id=receipt.settlement_id
            )
        function = "billing_import_legacy"
        statement = "SELECT clinic_app.billing_import_legacy(%s,%s)"
        arguments: list[UUID | None] = [setup.clinic_id, receipt.settlement_id]
        expected: tuple[object, ...] = (journal.pk,)
    else:
        terms = invoice_terms(setup)
        with finance_scope(setup):
            terms.refresh_from_db()
            appointment = Invoice.objects.get(pk=terms.invoice_id).appointment_id
        assert terms.completion_entry_id is not None
        if kind == "appointment":
            actor = graph.physician
            function = "billing_complete_appointment"
            statement = "SELECT * FROM clinic_app.billing_complete_appointment(%s,%s)"
            arguments = [setup.clinic_id, appointment]
            expected = (terms.completion_entry_id, False, terms.pk, False)
        else:
            function = "billing_recognize_completion"
            statement = (
                "SELECT * FROM clinic_app.billing_recognize_completion(%s,%s,%s)"
            )
            arguments = [setup.clinic_id, terms.invoice_id, None]
            expected = (terms.completion_entry_id, False)
    decisions = []
    for active in (True, False, True):
        _active(setup, actor, active=active)
        with permission_context(graph, actor), connection.cursor() as cursor:
            if active:
                cursor.execute(statement, arguments)
                assert cursor.fetchall() == [expected]
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    cursor.execute(statement, arguments)
                _sql_denial(denied.value, function)
                decisions.append(False)
    assert decisions == [True, False, True]
