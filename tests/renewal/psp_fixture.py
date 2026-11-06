"""Synthetic PSP browser fixtures use domain services as the real runtime role."""

from __future__ import annotations

import json
import sys
from uuid import UUID, uuid4

from apps.billing.compensation import prepare_invoice_terms
from apps.billing.journal import create_book
from apps.billing.money_types import NO_ADJUSTMENTS
from apps.billing.psp_contracts import FIXTURE_PROVIDER, PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspEvidence, PspIntent
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import reconcile_psp_intent
from apps.billing.psp_services import prepare_psp_charge, set_psp_policy
from apps.billing.psp_worker import register_payment_provider
from apps.billing.services import create_invoice, issue_invoice, release_invoice
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation
from apps.identity.models import UserClinicRole
from apps.providers import lifecycle
from apps.providers.services import current_version
from apps.tenancy.db import tenant_context

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from provider_gate_support import APPROVER


def main() -> None:
    """Configure local fixtures only; no external approval or provider is used."""
    data = json.loads(sys.stdin.read())
    organization, clinic, actor = (
        UUID(data[key]) for key in ("organization", "clinic", "actor")
    )
    if data["action"] == "prepare":
        method = data["method"]
        with owner_context(organization):
            UserClinicRole.objects.get_or_create(
                organization_id=organization,
                clinic_id=clinic,
                user_id=actor,
                role="finance",
            )
            capability = "pix" if method == "pix" else "psp_card"
            version = current_version(capability, clinic_id=clinic)
            if version is None or version.provider != FIXTURE_PROVIDER:
                version = lifecycle.propose_version(
                    capability,
                    clinic_id=clinic,
                    version_input=lifecycle.VersionInput(
                        provider=FIXTURE_PROVIDER,
                        environment="sandbox",
                        account="fixture",
                    ),
                )
                lifecycle.approve_version(
                    capability, clinic_id=clinic, decision=APPROVER
                )
                version.refresh_from_db()
                version.state = "sandbox"
                version.save(update_fields=("state", "updated_at"))
        with runtime_role(), tenant_context(actor, organization):
            create_book(clinic_id=clinic)
            set_psp_policy(clinic_id=clinic, max_installments=3, command_id=uuid4())
            invoice = create_invoice(
                clinic_id=clinic,
                patient_id=UUID(data["patient"]),
                amount_minor=18000,
                idempotency_key=uuid4(),
            )
            issue_invoice(
                clinic_id=clinic,
                invoice_id=invoice.pk,
                expected_revision=invoice.revision,
            )
            release_invoice(clinic_id=clinic, invoice_id=invoice.pk)
            prepare_invoice_terms(
                clinic_id=clinic,
                invoice_id=invoice.pk,
                payer="patient",
                adjustments=NO_ADJUSTMENTS,
                payout_rule_id=None,
            )
            intent = prepare_psp_charge(
                clinic_id=clinic,
                invoice_id=invoice.pk,
                method=method,
                installments=3 if method == "card" else 1,
            )
            operation_id = IntegrationOperation.objects.get(subject_id=intent.pk).pk
        with runtime_role():
            assert execute_operation(operation_id) == "succeeded"
        result = {"invoice": str(invoice.pk), "intent": str(intent.pk)}
    else:
        assert data["action"] == "settle"
        intent_id = UUID(data["intent"])
        with runtime_role(), tenant_context(actor, organization):
            intent = PspIntent.objects.get(pk=intent_id)
        register_payment_provider(
            RecordedPaymentProvider(
                secret=b"synthetic-browser-psp",
                records={
                    intent_id: PaymentStatus(
                        f"fixture-{intent_id.hex}", "settled", 18000, "BRL", 2
                    )
                },
            )
        )
        with runtime_role():
            assert reconcile_psp_intent(intent_id=intent_id) == "verified"
        with runtime_role(), tenant_context(actor, organization):
            evidence = PspEvidence.objects.get(intent=intent, decision="verified")
            receipt = apply_psp_evidence(clinic_id=clinic, evidence_id=evidence.pk)
            assert (
                apply_psp_evidence(clinic_id=clinic, evidence_id=evidence.pk) == receipt
            )
        result = {"receipt": str(receipt)}
    sys.stdout.write(json.dumps(result) + "\n")
