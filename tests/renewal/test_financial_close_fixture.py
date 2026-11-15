"""Real-role regression for the fiscal and insurer facts in the J6 close."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import FiscalDocument, InvoiceTerms
from apps.identity.models import User, UserClinicRole
from apps.insurance.models import PaymentMatch
from apps.intake.models import PatientClinicEnrollment

from billing.journal_support import finance_scope, finance_setup, invoice_terms
from identity.permission_support import owner_context
from renewal.financial_close_fixture import reconcile_documents

if TYPE_CHECKING:
    from billing.journal_support import FinanceSetup

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_close_documents_keep_the_same_clinic_patient_and_journal(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    terms = invoice_terms(setup)
    with owner_context(setup.graph.organization_a):
        receptionist = User.objects.create(username=f"synthetic-j6-{uuid4().hex}")
        UserClinicRole.objects.create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.clinic_id,
            user=receptionist,
            role="receptionist",
        )
    with finance_scope(setup):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=setup.clinic_id, patient_id=setup.appointment.patient_id
        )
    receipt = reconcile_documents(
        {
            "clinic": str(setup.clinic_id),
            "organization": str(setup.graph.organization_a),
            "actor": str(setup.graph.clinic_admin),
            "physician": str(setup.graph.physician),
            "receptionist": str(receptionist.pk),
            "patient": str(setup.appointment.patient_id),
            "enrollment": str(enrollment.pk),
            "invoice": str(terms.invoice_id),
        }
    )
    assert receipt == {
        "fiscal_authorized": 1,
        "insurer_charged_minor": 12345,
        "insurer_collected_minor": 8345,
        "insurer_outstanding_minor": 4000,
        "return_replay_idempotent": True,
        "payment_replay_idempotent": True,
    }
    with finance_scope(setup):
        fiscal = FiscalDocument.objects.get(clinic_id=setup.clinic_id)
        assert fiscal.invoice_id == terms.invoice_id
        assert fiscal.book_id == setup.book_id
        match = PaymentMatch.objects.get(clinic_id=setup.clinic_id)
        assert match.guide.membership.enrollment_id == enrollment.pk
        assert match.settlement.book_id == setup.book_id
        assert match.allocation.terms.invoice_id == match.guide.invoice_id
        insurer_terms = InvoiceTerms.objects.get(invoice_id=match.guide.invoice_id)
        assert insurer_terms.net_minor - match.amount_minor == 4000
