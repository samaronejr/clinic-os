"""PIX unavailable/synthetic acceptance; never provider integration evidence."""

from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import qrcode
from apps.billing.adapters import (
    PixResponseError,
    PixUnavailableError,
    SyntheticPixAdapter,
    pix_capability,
)
from apps.billing.models import Invoice, PixCharge, PixOperation, Receipt, Settlement
from apps.billing.pix import complete_pix_charge, prepare_pix_charge
from apps.billing.services import (
    BillingAccessDeniedError,
    BillingConflictError,
    cancel_invoice,
    create_invoice,
    issue_invoice,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone
from psycopg import sql

from appointment_service_support import seed_appointment_setup
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.billing.adapters import PixRequest, PixResponse
    from pytest_django.fixtures import SettingsWrapper

    from appointment_service_support import AppointmentSetup
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
TABLES = ["billing_pixoperation", "billing_pixcharge"]


@pytest.fixture(autouse=True)
def synthetic(settings: SettingsWrapper) -> None:
    settings.CLINIC_DATA_MODE = "synthetic"
    settings.BILLING_SYNTHETIC_PIX = True


@pytest.fixture
def issued(rbac_graph: RbacGraph) -> tuple[AppointmentSetup, Invoice]:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.patient_id,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=setup.clinic_id, invoice_id=invoice.pk, expected_revision=1
        )
    return setup, invoice


def prepare(setup: AppointmentSetup, invoice: Invoice) -> PixOperation:
    return prepare_pix_charge(clinic_id=setup.clinic_id, invoice_id=invoice.pk)


def complete(
    setup: AppointmentSetup, invoice: Invoice, operation: PixOperation
) -> PixCharge:
    return complete_pix_charge(
        clinic_id=setup.clinic_id, invoice_id=invoice.pk, operation_id=operation.pk
    )


def test_exact_synthetic_charge_replays_without_settlement(
    issued: tuple[AppointmentSetup, Invoice],
) -> None:
    setup, invoice = issued
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = prepare(setup, invoice)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        charge = complete(setup, invoice, operation)
        assert prepare(setup, invoice).pk == operation.pk
        repeated = complete(setup, invoice, operation)
        assert repeated.pk == charge.pk
        assert repeated.qr_base64 == charge.qr_base64
        assert (operation.amount_minor, operation.currency) == (12345, "BRL")
        assert operation.invoice_reference == invoice.reference
        assert operation.expires_at - operation.created_at == timedelta(minutes=30)
        assert charge.synthetic is True
        assert charge.provider_reference == f"synthetic-pix-{operation.pk}"
        assert charge.copy_code == (
            f"SYNTHETIC-NOT-PAYABLE|synthetic-pix-{operation.pk}|{invoice.reference}|"
            f"12345|BRL|{operation.expires_at.isoformat()}"
        )
        expected = io.BytesIO()
        qrcode.make(charge.copy_code).save(expected)
        assert base64.b64decode(charge.qr_base64, validate=True) == expected.getvalue()
        assert PixOperation.objects.count() == PixCharge.objects.count() == 1
        assert not Receipt.objects.exists()
        assert not Settlement.objects.exists()
        invoice.refresh_from_db()
        assert invoice.state == "open"
    assert pix_capability().real_enabled is False


@pytest.mark.parametrize("provider", ["asaas", "asaas-sandbox", "production"])
def test_unapproved_providers_cannot_be_enabled(
    issued: tuple[AppointmentSetup, Invoice], provider: str, settings: SettingsWrapper
) -> None:
    setup, invoice = issued
    settings.BILLING_PIX_REAL_ENABLED = True
    settings.ASAAS_API_KEY = "synthetic-not-a-credential"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(PixUnavailableError):
            prepare_pix_charge(
                clinic_id=setup.clinic_id, invoice_id=invoice.pk, provider=provider
            )
        assert not PixOperation.objects.exists()
    assert pix_capability().real_enabled is False


@pytest.mark.parametrize(("mode", "enabled"), [("live", True), ("synthetic", False)])
def test_gate_fails_closed_without_rehearsal_authority(
    issued: tuple[AppointmentSetup, Invoice],
    settings: SettingsWrapper,
    mode: str,
    enabled: bool,
) -> None:
    setup, invoice = issued
    settings.CLINIC_DATA_MODE = mode
    settings.BILLING_SYNTHETIC_PIX = enabled
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(PixUnavailableError):
            prepare(setup, invoice)
        with pytest.raises(PixUnavailableError):
            complete_pix_charge(
                clinic_id=setup.clinic_id, invoice_id=invoice.pk, operation_id=uuid4()
            )
        assert not PixOperation.objects.exists()


@pytest.mark.parametrize(
    "alter",
    [
        lambda r: replace(r, amount_minor=12346),
        lambda r: replace(r, currency="USD"),
        lambda r: replace(r, invoice_reference=uuid4()),
        lambda r: replace(r, operation_id=uuid4()),
        lambda r: replace(r, provider_reference="untracked"),
        lambda r: replace(r, expires_at=r.expires_at + timedelta(seconds=1)),
        lambda r: replace(r, copy_code="malformed"),
        lambda r: replace(r, qr_base64="not-base64"),
        lambda r: replace(r, synthetic=False),
    ],
)
def test_malformed_or_mismatched_response_preserves_pending_request(
    issued: tuple[AppointmentSetup, Invoice],
    monkeypatch: pytest.MonkeyPatch,
    alter: Callable[[PixResponse], PixResponse],
) -> None:
    setup, invoice = issued
    original = SyntheticPixAdapter.create_charge

    def malformed(self: SyntheticPixAdapter, request: PixRequest) -> PixResponse:
        return alter(original(self, request))

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = prepare(setup, invoice)
    monkeypatch.setattr(SyntheticPixAdapter, "create_charge", malformed)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(PixResponseError):
            complete(setup, invoice, operation)
        assert PixOperation.objects.count() == 1
        assert not PixCharge.objects.exists()
    monkeypatch.setattr(SyntheticPixAdapter, "create_charge", original)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert complete(setup, invoice, operation).operation_id == operation.pk


def test_timeout_after_generation_replays_exact_local_result(
    issued: tuple[AppointmentSetup, Invoice], monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, invoice = issued
    generated: list[PixResponse] = []
    original = SyntheticPixAdapter.create_charge

    def interrupted(self: SyntheticPixAdapter, request: PixRequest) -> PixResponse:
        generated.append(original(self, request))
        raise TimeoutError

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = prepare(setup, invoice)
    monkeypatch.setattr(SyntheticPixAdapter, "create_charge", interrupted)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(TimeoutError):
            complete(setup, invoice, operation)
        assert not PixCharge.objects.exists()
        assert prepare(setup, invoice).pk == operation.pk
    monkeypatch.setattr(SyntheticPixAdapter, "create_charge", original)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        result = complete(setup, invoice, operation)
        assert result.provider_reference == generated[0].provider_reference
        assert result.copy_code == generated[0].copy_code
        assert result.qr_base64 == generated[0].qr_base64
        assert PixCharge.objects.count() == 1


@pytest.mark.parametrize("completed", [True, False])
def test_expiration_creates_linked_operation_without_editing_history(
    issued: tuple[AppointmentSetup, Invoice],
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    setup, invoice = issued
    # Time is the behavior under test. Create a historical fixture without sleeps.
    past = timezone.now() - timedelta(hours=1)
    with monkeypatch.context() as clock:
        clock.setattr("apps.billing.pix.timezone.now", lambda: past)
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            original = prepare(setup, invoice)
            charge = complete(setup, invoice, original) if completed else None
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert prepare(setup, invoice).pk == original.pk
        successor = prepare_pix_charge(
            clinic_id=setup.clinic_id, invoice_id=invoice.pk, previous_id=original.pk
        )
        assert successor.pk != original.pk
        assert successor.previous_id == original.pk
        assert successor.amount_minor == original.amount_minor
        assert successor.expires_at > original.expires_at
        assert (
            prepare_pix_charge(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                previous_id=original.pk,
            ).pk
            == successor.pk
        )
        with pytest.raises(BillingConflictError):
            prepare_pix_charge(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                previous_id=successor.pk,
            )
        complete(setup, invoice, successor)
        if charge is not None:
            original_result = complete(setup, invoice, original)
            assert original_result.copy_code == charge.copy_code
            assert original_result.qr_base64 == charge.qr_base64
        else:
            with pytest.raises(BillingConflictError):
                complete(setup, invoice, original)
        original.refresh_from_db()
        assert original.expires_at == past + timedelta(minutes=30)
        assert PixOperation.objects.count() == 2


def test_parallel_retries_converge(
    issued: tuple[AppointmentSetup, Invoice],
) -> None:
    setup, invoice = issued
    barrier = Barrier(2)

    def worker() -> tuple[UUID, str]:
        try:
            barrier.wait(timeout=10)
            with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
                operation = prepare(setup, invoice)
            with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
                result = complete(setup, invoice, operation)
                return operation.pk, result.provider_reference
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert PixOperation.objects.count() == PixCharge.objects.count() == 1


def test_scope_closed_invoice_and_immutable_history(
    issued: tuple[AppointmentSetup, Invoice], rbac_graph: RbacGraph
) -> None:
    setup, invoice = issued
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = prepare(setup, invoice)
        charge = complete(setup, invoice, operation)
        for model in (PixOperation, PixCharge):
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.all().update(invoice_id=uuid4())
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.all().delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            PixOperation.objects.create(
                organization_id=setup.organization_id,
                invoice=invoice,
                invoice_reference=invoice.reference,
                amount_minor=1,
                currency="BRL",
                expires_at=operation.expires_at,
            )
        with pytest.raises(BillingConflictError):
            complete_pix_charge(
                clinic_id=setup.clinic_id, invoice_id=invoice.pk, operation_id=uuid4()
            )
        cancel_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
        with pytest.raises(BillingConflictError):
            prepare(setup, invoice)
        with pytest.raises(BillingConflictError):
            complete(setup, invoice, operation)
        assert PixCharge.objects.get(pk=charge.pk).copy_code == charge.copy_code
    for actor, organization in [
        (setup.practitioner_id, setup.organization_id),
        (rbac_graph.clinic_admin, rbac_graph.organization_a),
        (rbac_graph.shared_user, rbac_graph.organization_b),
    ]:
        with runtime_role(), tenant_context(actor, organization):
            assert not PixOperation.objects.exists()
            assert not PixCharge.objects.exists()
            with pytest.raises(BillingAccessDeniedError):
                prepare(setup, invoice)
    with runtime_role():
        assert not PixOperation.objects.exists()
        assert not PixCharge.objects.exists()


def test_exact_database_rls_grants_and_function_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, "
            "relowner::regrole::text FROM pg_class "
            "WHERE relnamespace = 'clinic_app'::regnamespace AND relname = ANY(%s)",
            [TABLES],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)",
            [TABLES],
        )
        assert set(cursor.fetchall()) == {(table, "billing_staff") for table in TABLES}
        cursor.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.table_privileges "
            "WHERE table_schema = 'clinic_app' AND table_name = ANY(%s) "
            "AND grantee = 'clinic_app'",
            [TABLES],
        )
        assert set(cursor.fetchall()) == {
            (table, privilege) for table in TABLES for privilege in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT has_function_privilege('clinic_app', "
            "'clinic_app.billing_pix_guard()', 'EXECUTE')"
        )
        assert cursor.fetchone() == (False,)
    with runtime_role(), connection.cursor() as cursor:
        for table in TABLES:
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    sql.SQL("DELETE FROM clinic_app.{}").format(sql.Identifier(table))
                )
