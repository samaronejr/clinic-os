"""Delivery identity never replaces the provider's versioned economic fact."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import InvoiceTerms, JournalEntry, JournalSettlement
from apps.billing.money_types import JournalConflictError
from apps.billing.payouts import approve_payout, request_payout
from apps.billing.psp_contracts import StatementLine
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspBinding, PspEvidence, PspIntent, PspReceipt
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import (
    receive_psp_event,
    reconcile_psp_intent,
    reconcile_psp_statement,
)
from apps.billing.psp_services import prepare_psp_outgoing
from apps.billing.psp_worker import register_payment_provider
from apps.billing.security import finance_session
from apps.comms.models import IntegrationOperation
from apps.core import integration

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    fresh_finance_session,
)
from billing.test_psp_adapter import PAID, SECRET, signed_event
from billing.test_psp_review import prepared_payment
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.billing.psp_contracts import PaymentStatus

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("source", ["duplicate-callback", "bad-statement", "no-lines"])
@pytest.mark.parametrize("dispatched", [False, True])
def test_new_lookup_blocks_stale_collection_despite_delivery_disposition(
    prepared_payment: tuple[FinanceSetup, PspIntent], source: str, dispatched: bool
) -> None:
    setup, intent = prepared_payment
    headers, body = signed_event()
    register_payment_provider(
        RecordedPaymentProvider(secret=SECRET, records={intent.pk: PAID})
    )
    with finance_scope(setup):
        operation_id = IntegrationOperation.objects.get(subject_id=intent.pk).pk
    with runtime_role():
        # Establish the provider reference and retain one settled event, but
        # deliberately do not let Finance apply it before the provider reverses.
        if dispatched:
            assert integration.execute_operation(operation_id) == "succeeded"
        else:
            assert reconcile_psp_intent(intent_id=intent.pk) == "verified"
        assert (
            receive_psp_event(provider=intent.provider, headers=headers, body=body)
            == "verified"
        )
    register_payment_provider(
        RecordedPaymentProvider(
            secret=SECRET,
            records={intent.pk: replace(PAID, status="reversed", version=3)},
            statements=()
            if source == "no-lines"
            else (StatementLine("bad-line", PAID.provider_reference, 9999, "BRL"),),
        )
    )
    with runtime_role():
        if source == "duplicate-callback":
            assert (
                receive_psp_event(provider=intent.provider, headers=headers, body=body)
                == "duplicate"
            )
        else:
            result = reconcile_psp_statement(intent_id=intent.pk)
            assert result == (
                ("unmatched",) if source == "no-lines" else ("quarantined",)
            )
    with finance_scope(setup):
        stale = PspEvidence.objects.get(intent=intent, source="callback")
        assert (stale.decision, stale.version) == ("verified", 2)
        with pytest.raises(JournalConflictError):
            apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=stale.pk)
        reversal = PspEvidence.objects.get(intent=intent, source="status", version=3)
        assert (reversal.status, reversal.amount_minor) == ("reversed", 10000)
        assert PspBinding.objects.filter(intent=intent).count() == 1
        assert not JournalSettlement.objects.exists()
        assert not JournalEntry.objects.filter(source_kind="collection").exists()
        assert not PspReceipt.objects.exists()


def _install_status(intent: PspIntent, status: PaymentStatus | None) -> str:
    register_payment_provider(
        RecordedPaymentProvider(
            secret=SECRET, records={intent.pk: status} if status else {}
        )
    )
    with runtime_role():
        return reconcile_psp_intent(intent_id=intent.pk)


def test_older_unknown_cannot_advance_an_outgoing_instruction(
    prepared_payment: tuple[FinanceSetup, PspIntent],
) -> None:
    setup, _ = prepared_payment
    key = fresh_finance_session(setup)
    # Use the prepared invoice, not a second appointment in its slot.
    with (
        finance_scope(setup),
        finance_session(clinic_id=setup.clinic_id, session_key=key),
    ):
        allocated(setup, InvoiceTerms.objects.get(book_id=setup.book_id), 1000)
        payout = request_payout(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.graph.physician,
            amount_minor=300,
            command_id=uuid4(),
        )
        approve_payout(clinic_id=setup.clinic_id, payout_id=payout.pk)
        intent = prepare_psp_outgoing(
            clinic_id=setup.clinic_id, kind="payout", target_id=payout.pk
        )
    assert _install_status(intent, None) == "unknown"
    assert (
        _install_status(intent, replace(PAID, status="confirmed", amount_minor=300))
        == "verified"
    )
    with (
        finance_scope(setup),
        finance_session(clinic_id=setup.clinic_id, session_key=key),
    ):
        stale = PspEvidence.objects.get(intent=intent, decision="unknown")
        with pytest.raises(JournalConflictError):
            apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=stale.pk)
        payout.refresh_from_db()
        assert payout.state == "sending"
