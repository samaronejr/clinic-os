"""Task 57 happy, timeout, pre-scope authentication and immutable-history probes."""

import hmac
import json
import os
from pathlib import Path

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.billing.models import JournalEntry, JournalSettlement
from apps.billing.psp_contracts import (
    FIXTURE_PROVIDER,
    PaymentAuthenticationError,
    PaymentStatus,
    StatementLine,
)
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspEvidence, PspReceipt
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import (
    receive_psp_event,
    reconcile_psp_intent,
    reconcile_psp_statement,
)
from apps.billing.psp_services import prepare_psp_charge
from apps.billing.psp_storage import DatabasePaymentEventStore
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from billing.journal_support import finance_scope, finance_setup, invoice_terms
from billing.test_accounting_policy import balance
from billing.test_psp_outbox import EffectThenTimeout
from billing.test_psp_reconciliation import sandbox
from billing.test_psp_refunds import (
    test_replayed_refund_callback_requires_finance_and_reverses_repasse_once as refund_replay,
)
from identity.permission_support import owner_context
from infra.psp_catalog import TABLES
from patient_service_support import runtime_role

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)
SECRET = b"synthetic-manual-qa-only"
SENTINEL = "SINTETICO-SENTINELA-PSP-57"
E = Path(os.environ["T57_EVIDENCE"])


def save(name, result):
    (E / name).write_text(json.dumps({"synthetic": True, **result}, indent=2) + "\n")


def prepared(setup, settings, monkeypatch):
    sandbox(setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(setup)
    with finance_scope(setup):
        intent = prepare_psp_charge(
            clinic_id=setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    return intent, operation


def paid(setup, settings, monkeypatch):
    intent, operation = prepared(setup, settings, monkeypatch)
    status = PaymentStatus(
        f"fixture-{intent.pk.hex}", "settled", 10000, "BRL", 2, 100
    )
    register_payment_provider(
        RecordedPaymentProvider(
            secret=SECRET,
            records={intent.pk: status},
            statements=(
                StatementLine(
                    "manual-statement-57", status.provider_reference,
                    10000, "BRL", 100,
                ),
            ),
        )
    )
    with runtime_role():
        assert reconcile_psp_intent(intent_id=intent.pk) == "verified"
    with finance_scope(setup):
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        receipt = apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=evidence.pk)
        assert apply_psp_evidence(
            clinic_id=setup.clinic_id, evidence_id=evidence.pk
        ) == receipt
    return intent, operation, status


def test_fixture_pix_paid_posts_once_and_statement_closes_clearing(
    finance_setup, settings, monkeypatch
):
    setup = finance_setup
    intent, operation = prepared(setup, settings, monkeypatch)
    register_payment_provider(RecordedPaymentProvider(secret=SECRET, records={}))
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "succeeded"
    with finance_scope(setup):
        assert PspEvidence.objects.get(intent=intent).status == "pending"
        assert not PspReceipt.objects.exists()
        assert not JournalSettlement.objects.exists()
    status = PaymentStatus(f"fixture-{intent.pk.hex}", "settled", 10000, "BRL", 2, 100)
    register_payment_provider(
        RecordedPaymentProvider(
            secret=SECRET, records={intent.pk: status},
            statements=(StatementLine(
                "manual-statement-57", status.provider_reference, 10000, "BRL", 100
            ),),
        )
    )
    with runtime_role():
        assert reconcile_psp_intent(intent_id=intent.pk) == "verified"
    with finance_scope(setup):
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        first = apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=evidence.pk)
        assert apply_psp_evidence(
            clinic_id=setup.clinic_id, evidence_id=evidence.pk
        ) == first
    with runtime_role():
        assert reconcile_psp_statement(intent_id=intent.pk) == ("matched",)
        assert reconcile_psp_statement(intent_id=intent.pk) == ("duplicate",)
    with finance_scope(setup):
        statement = PspEvidence.objects.get(intent=intent, source="statement")
        for _ in range(2):
            apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=statement.pk)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("clinic_app",)
        amounts = {
            account: balance(setup.book_id, account)
            for account in ("bank", "psp_clearing", "psp_fees", "practitioner_payables")
        }
        assert amounts == {
            "bank": 9900, "psp_clearing": 0, "psp_fees": 100,
            "practitioner_payables": -3000,
        }
        assert PspReceipt.objects.count() == JournalSettlement.objects.count() == 1
        assert JournalEntry.objects.filter(source_kind="collection").count() == 1
        save("pix.json", {
            "runtime_role": "clinic_app", "created_instructions_state": "pending",
            "instructions_created_receipt": False, "receipts": 1,
            "collection_entries": 1, "statement_reimport": "duplicate",
            "balances_minor": amounts,
        })


def test_psp_timeout_is_held_then_reconciled_under_the_original_key(
    finance_setup, settings, monkeypatch
):
    setup = finance_setup
    intent, operation = prepared(setup, settings, monkeypatch)
    provider = EffectThenTimeout()
    register_payment_provider(provider)
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "reconcile"
    with finance_scope(setup):
        operation.refresh_from_db()
        assert operation.status == "in_progress"
        assert PspEvidence.objects.get(intent=intent).decision == "unknown"
        assert not PspReceipt.objects.exists()
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "succeeded"
    with finance_scope(setup):
        operation.refresh_from_db()
        assert operation.attempt_count == provider.effects == 1
    save("timeout.json", {
        "runtime_role": "clinic_app", "timeout_decision": "unknown",
        "held_status": "in_progress", "recovery": "succeeded",
        "remote_effects": provider.effects, "send_attempts": operation.attempt_count,
    })


def test_authentication_tenant_privacy_replay_and_immutable_history(
    finance_setup, settings, monkeypatch, superuser_database_url, caplog
):
    setup = finance_setup
    intent, operation, status = paid(setup, settings, monkeypatch)
    payload = {
        "event_id": "manual-callback-57", "provider_reference": status.provider_reference,
        "status": "settled", "amount_minor": 10000, "currency": "BRL", "version": 2,
        "tenant_id": str(setup.graph.organization_b), "clinical_note": SENTINEL,
    }
    body = json.dumps(payload).encode()
    headers = {"x-fixture-psp-signature": hmac.digest(SECRET, body, "sha256").hex()}
    with monkeypatch.context() as patch:
        patch.setattr(
            DatabasePaymentEventStore, "resolve",
            lambda *args: pytest.fail("forged raw bytes reached stored scope"),
        )
        with runtime_role(), pytest.raises(PaymentAuthenticationError):
            receive_psp_event(provider=FIXTURE_PROVIDER, headers=headers, body=body+b" ")
    with runtime_role():
        assert receive_psp_event(
            provider=FIXTURE_PROVIDER, headers=headers, body=body
        ) == "verified"
        assert receive_psp_event(
            provider=FIXTURE_PROVIDER, headers=headers, body=body
        ) == "duplicate"
        with tenant_context(setup.graph.shared_user, setup.graph.organization_b):
            with connection.cursor() as cursor:
                for table in sorted(TABLES):
                    cursor.execute(f"SELECT count(*) FROM clinic_app.{table}")
                    assert cursor.fetchone() == (0,)
    with finance_scope(setup):
        for table in sorted(TABLES):
            for verb in ("DELETE FROM", "TRUNCATE"):
                with pytest.raises(DatabaseError), transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(f"{verb} clinic_app.{table}")
        assert PspReceipt.objects.count() == 1
        assert JournalSettlement.objects.count() == 1
    with owner_context(setup.graph.organization_a):
        audit = list(AuditEvent.objects.values_list("payload", flat=True))
        outbox = list(IntegrationOperation.objects.values())
        assert SENTINEL not in json.dumps(audit, default=str)
        assert SENTINEL not in json.dumps(outbox, default=str)
        assert SENTINEL not in caplog.text
    for table in sorted(TABLES):
        with psycopg.connect(superuser_database_url) as privileged:
            with pytest.raises(psycopg.Error) as refused:
                privileged.execute(f"DELETE FROM clinic_app.{table}")
            assert refused.value.sqlstate == "23514"
    save("adversarial.json", {
        "runtime_role": "clinic_app", "forged_callback_pre_scope_refused": True,
        "payload_tenant_ignored": True, "replay": "duplicate",
        "cross_org_tables_denied": sorted(TABLES),
        "runtime_delete_truncate_denied": True,
        "privileged_delete_trigger_denied": True,
        "audit_outbox_logs_sentinel_free": True,
    })


def test_manual_authenticated_refund_replay_has_one_linked_reversal(
    finance_setup, settings, monkeypatch
):
    refund_replay(finance_setup, settings, monkeypatch)
    with finance_scope(finance_setup):
        callback_facts = PspEvidence.objects.filter(
            intent__kind="refund", source="callback"
        ).count()
        refunds = JournalEntry.objects.filter(source_kind="refund_confirmed").count()
        repasse = JournalEntry.objects.filter(source_kind="repasse_reversal").count()
        assert callback_facts == refunds == repasse == 1
    save("refund.json", {
        "runtime_role": "clinic_app", "authenticated_callback": "verified",
        "replayed_callback": "duplicate", "refund_callback_facts": callback_facts,
        "refund_entries": refunds, "linked_repasse_reversals": repasse,
        "refund_minor": 5000, "repasse_reversed_minor": 1500,
    })
