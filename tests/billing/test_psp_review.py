"""Regression proofs for evidence authority, rejected observations and recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.money_types import JournalConflictError
from apps.billing.psp_contracts import (
    PaymentStatus,
    PaymentUnavailableError,
    StatementLine,
)
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_gate import require_psp_sandbox
from apps.billing.psp_models import PspBinding, PspEvidence, PspIntent, PspReceipt
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import (
    reconcile_psp_intent,
    reconcile_psp_statement,
)
from apps.billing.psp_services import prepare_psp_charge
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.identity.models import UserClinicRole
from apps.providers import lifecycle
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from psycopg import Error as PostgresError

from auth.stepup_test_support import create_role_actor
from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_accounting_policy import balance
from billing.test_psp_outbox import EffectThenTimeout
from billing.test_psp_reconciliation import sandbox
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


@pytest.fixture
def prepared_payment(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FinanceSetup, PspIntent]:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
    return finance_setup, intent


@pytest.mark.parametrize("role", ["clinic_manager", "owner", "finance"])
def test_financial_read_authority_cannot_fabricate_another_actors_evidence(
    prepared_payment: tuple[FinanceSetup, PspIntent],
    role: str,
) -> None:
    setup, intent = prepared_payment
    reader = create_role_actor(setup.graph, UserClinicRole.Role(role))
    assert reader.pk != intent.requested_by_id
    errors = []
    with (
        runtime_role(),
        tenant_context(reader.pk, setup.graph.organization_a),
    ):
        assert PspIntent.objects.filter(pk=intent.pk).exists()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.has_permission(%s,%s,NULL)",
                ["charge.collect", str(setup.clinic_id)],
            )
            assert cursor.fetchone() == (role == "finance",)
        for target_id in (intent.pk, uuid4()):
            for insert in (
                lambda target_id=target_id: PspBinding.objects.create(
                    organization_id=intent.organization_id,
                    intent_id=target_id,
                    provider=intent.provider,
                    provider_reference="fixture-forged-binding",
                ),
                lambda target_id=target_id: PspEvidence.objects.create(
                    organization_id=intent.organization_id,
                    intent_id=target_id,
                    provider=intent.provider,
                    event_id="fixture-forged-evidence",
                    source="status",
                    status="settled",
                    decision="verified",
                    amount_minor=10000,
                    currency="BRL",
                    version=2,
                ),
            ):
                with pytest.raises(DatabaseError) as refused, transaction.atomic():
                    insert()
                cause = refused.value.__cause__
                assert isinstance(cause, PostgresError)
                errors.append((cause.sqlstate, cause.diag.message_primary))
    assert errors == [("42501", "billing access denied")] * 4


def test_corrected_statement_at_the_same_provider_version_can_post(
    prepared_payment: tuple[FinanceSetup, PspIntent],
) -> None:
    setup, intent = prepared_payment
    status = PaymentStatus(
        "fixture-corrected-statement", "settled", 10000, "BRL", 2, 100
    )
    for amount, line_id, decision in (
        (9999, "fixture-wrong-statement", "quarantined"),
        (10000, "fixture-correct-statement", "matched"),
    ):
        register_payment_provider(
            RecordedPaymentProvider(
                secret=b"synthetic-statement-correction",
                records={intent.pk: status},
                statements=(
                    StatementLine(
                        line_id, status.provider_reference, amount, "BRL", 100
                    ),
                ),
            )
        )
        with runtime_role():
            assert reconcile_psp_statement(intent_id=intent.pk) == (decision,)
    with finance_scope(setup):
        evidence = PspEvidence.objects.get(intent=intent, decision="matched")
        apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=evidence.pk)
        assert balance(setup.book_id, "bank") == 9900
        assert balance(setup.book_id, "psp_fees") == 100
        assert balance(setup.book_id, "psp_clearing") == 0
        assert PspReceipt.objects.count() == 1


def test_newer_authoritative_reversal_still_blocks_prior_verified_fact(
    prepared_payment: tuple[FinanceSetup, PspIntent],
) -> None:
    setup, intent = prepared_payment
    for status, decision in (
        (
            PaymentStatus("fixture-status-reversal", "settled", 10000, "BRL", 2),
            "verified",
        ),
        (
            PaymentStatus("fixture-status-reversal", "reversed", 10000, "BRL", 3),
            "quarantined",
        ),
    ):
        register_payment_provider(
            RecordedPaymentProvider(
                secret=b"synthetic-status-reversal",
                records={intent.pk: status},
            )
        )
        with runtime_role():
            assert reconcile_psp_intent(intent_id=intent.pk) == decision
    with finance_scope(setup):
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        with pytest.raises(JournalConflictError):
            apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=evidence.pk)
        assert not PspReceipt.objects.exists()


def test_lookup_only_recovery_retains_original_approved_version_after_rotation(
    prepared_payment: tuple[FinanceSetup, PspIntent],
) -> None:
    setup, intent = prepared_payment
    provider = EffectThenTimeout()
    register_payment_provider(provider)
    with finance_scope(setup):
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "reconcile"
    with owner_context(setup.graph.organization_a):
        lifecycle.propose_version(
            "pix",
            clinic_id=setup.clinic_id,
            version_input=lifecycle.VersionInput(
                provider="synthetic-replacement", account="rotation"
            ),
        )
    with pytest.raises(PaymentUnavailableError):
        require_psp_sandbox(
            clinic_id=setup.clinic_id,
            method="pix",
            version_id=intent.capability_version_id,
        )
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "succeeded"
    with finance_scope(setup):
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=evidence.pk)
        operation.refresh_from_db()
        assert operation.attempt_count == provider.effects == 1
        assert PspReceipt.objects.count() == 1
