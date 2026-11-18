"""Todo 72, IS-16: scheduling, charting and ordinary billing with every fault on."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import Invoice, Receipt
from apps.billing.services import confirm_settlement, create_invoice, issue_invoice
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import create_draft, open_encounter, record_clinical_note
from apps.tenancy.db import tenant_context
from config.settings.contracts import SYNTHETIC_FAULT_FAMILIES

from patient_service_support import runtime_role
from renewal.test_encounters import (
    CONTENT,
    seed_appointment_setup_for_existing,
)
from renewal.test_encounters import seed as clinical_seed
from scheduling.appointment_service_support import create_synthetic_appointment

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("mode", ["down", "slow", "ambiguous"])
def test_core_work_needs_no_provider_while_every_family_is_faulted(
    rbac_graph: RbacGraph, settings: SettingsWrapper, mode: str
) -> None:
    # Given: every synthetic provider family faulted at once.
    settings.CLINIC_SYNTHETIC_FAULTS = dict.fromkeys(SYNTHETIC_FAULT_FAMILIES, mode)
    settings.BILLING_SYNTHETIC_PIX = True
    settings.COMMS_SYNTHETIC_CHANNELS = ("email", "sms", "whatsapp")
    settings.TELECONSULT_SYNTHETIC_PROVIDER = True
    settings.PRESCRIPTION_SYNTHETIC_SIGNING = True
    graph = rbac_graph
    appointment, template = clinical_seed(graph)
    setup = seed_appointment_setup_for_existing(graph, appointment)
    # When/Then: reception still books, the physician still charts...
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        booked = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
        assert booked.status == "scheduled"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
        draft = create_draft(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template.pk
        )
        note = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=draft.revision,
            content=CONTENT,
        )
        stored = ClinicalDocumentVersion.objects.get(pk=note.pk)
        assert {field: getattr(stored, field) for field in CONTENT} == CONTENT
    # ...and billing still invoices and records a manually confirmed payment.
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = create_invoice(
            clinic_id=graph.clinic_a,
            patient_id=appointment.patient_id,
            amount_minor=15000,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=graph.clinic_a, invoice_id=invoice.pk, expected_revision=1
        )
        receipt = confirm_settlement(
            clinic_id=graph.clinic_a,
            invoice_id=invoice.pk,
            confirmation_reference=uuid4(),
            amount_minor=15000,
        )
        assert Receipt.objects.filter(pk=receipt.pk).exists()
        assert Invoice.objects.get(pk=invoice.pk).state != Invoice.State.OPEN
