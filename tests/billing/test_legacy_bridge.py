"""Historical one-to-one receipts retain additive many-to-many journal lineage."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.billing.models import (
    InvoiceTerms,
    JournalEntry,
    JournalSettlement,
    Receipt,
    SettlementAllocation,
)
from apps.billing.pix import complete_pix_charge, prepare_pix_charge
from apps.billing.services import confirm_settlement, create_invoice, issue_invoice
from apps.tenancy.db import tenant_context

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_accounting_policy import balance
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_payment_reconciliation import (
    _deliver,
    _signed_event,
    scripted,
    synthetic,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup", "scripted", "synthetic")


def test_first_legacy_receipt_prepares_missing_terms_and_commits_the_bridge(
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup):
        # Given: an issued charge has no negotiated or completion terms.
        invoice = create_invoice(
            clinic_id=finance_setup.clinic_id,
            patient_id=finance_setup.appointment.patient_id,
            amount_minor=1000,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=finance_setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        assert not InvoiceTerms.objects.filter(invoice=invoice).exists()
        # When: the old full-settlement API also prepares the journal lineage.
        receipt = confirm_settlement(
            clinic_id=finance_setup.clinic_id,
            invoice_id=invoice.pk,
            confirmation_reference=uuid4(),
            amount_minor=1000,
        )
        receipt_id = receipt.pk
    with finance_scope(finance_setup):
        # Then: audit registration did not roll back the receipt or money.
        terms = InvoiceTerms.objects.get(invoice=invoice)
        stored = Receipt.objects.get(pk=receipt_id)
        bridge = JournalSettlement.objects.get(
            legacy_settlement_id=stored.settlement_id
        )
        assert SettlementAllocation.objects.get(settlement=bridge).terms_id == terms.pk
        assert balance(finance_setup.book_id, "bank") == 1000
        assert balance(finance_setup.book_id, "patient_advances") == -1000


def test_full_legacy_receipt_after_partial_collection_keeps_excess_in_advances(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a completed 100 reais service has already collected 50.
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        allocated(finance_setup, terms, 5000)
        reference = uuid4()
        # When: the existing full-manual-settlement API attests another real 100.
        receipt = confirm_settlement(
            clinic_id=finance_setup.clinic_id,
            invoice_id=terms.invoice_id,
            confirmation_reference=reference,
            amount_minor=10000,
        )
        repeated = confirm_settlement(
            clinic_id=finance_setup.clinic_id,
            invoice_id=terms.invoice_id,
            confirmation_reference=reference,
            amount_minor=10000,
        )
        # Old one-to-one links persist; allocate only outstanding debt.
        assert repeated.pk == receipt.pk
        assert receipt.invoice_id == receipt.settlement.invoice_id == terms.invoice_id
        bridge = JournalSettlement.objects.get(legacy_settlement=receipt.settlement)
        edge = SettlementAllocation.objects.get(settlement=bridge)
        assert edge.amount_minor == 5000
        assert balance(finance_setup.book_id, "bank") == 15000
        assert balance(finance_setup.book_id, "patient_ar") == 0
        assert balance(finance_setup.book_id, "patient_advances") == -5000
        assert balance(finance_setup.book_id, "practitioner_payables") == -3000


@pytest.mark.usefixtures("scripted")
def test_distinct_authenticated_provider_events_post_one_economic_fact(
    finance_setup: FinanceSetup,
) -> None:
    # Given: the foundation's HMAC-authenticated scripted provider and a clinic book.
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_a
        ),
    ):
        invoice = create_invoice(
            clinic_id=finance_setup.clinic_id,
            patient_id=finance_setup.appointment.patient_id,
            amount_minor=1000,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=finance_setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        operation = prepare_pix_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=invoice.pk
        )
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_a
        ),
    ):
        charge = complete_pix_charge(
            clinic_id=finance_setup.clinic_id,
            invoice_id=invoice.pk,
            operation_id=operation.pk,
        )
    first = _signed_event(
        event_id="journal-settlement-first",
        provider_reference=charge.provider_reference,
        amount_minor=1000,
        extra_claims={"clinical_note": "SINTETICO-SENTINELA-T56"},
    )
    second = _signed_event(
        event_id="journal-settlement-second",
        provider_reference=charge.provider_reference,
        amount_minor=1000,
    )
    # When: the same settlement arrives under two transport ids, each redelivered.
    with runtime_role():
        assert [_deliver(*first), _deliver(*first)] == ["settled", "duplicate"]
        assert [_deliver(*second), _deliver(*second)] == ["recorded", "duplicate"]
    # Then: one receipt, bridge, allocation and collection entry survive COMMIT.
    with finance_scope(finance_setup):
        receipt = Receipt.objects.get(invoice=invoice)
        bridge = JournalSettlement.objects.get(
            legacy_settlement_id=receipt.settlement_id
        )
        assert JournalSettlement.objects.count() == 1
        assert SettlementAllocation.objects.get(settlement=bridge).amount_minor == 1000
        assert (
            JournalEntry.objects.filter(
                source_kind="collection", source_id=bridge.pk
            ).count()
            == 1
        )
        assert balance(finance_setup.book_id, "bank") == 1000
        assert balance(finance_setup.book_id, "patient_advances") == -1000
    with owner_context(finance_setup.graph.organization_a):
        payloads = tuple(AuditEvent.objects.values_list("payload", flat=True))
        assert payloads
        assert all(
            set(payload)
            <= {
                "clinic_id",
                "http_method",
                "http_status",
                "object_verb",
                "reason_code",
                "request_id",
            }
            for payload in payloads
        )
        assert "SINTETICO-SENTINELA-T56" not in str(payloads)
