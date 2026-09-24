"""Task 39 acceptance: authenticated payment events reconcile exactly once."""

from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from threading import Barrier, Event, current_thread
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.billing.adapters import (
    AuthenticatedPaymentEvent,
    PaymentEventAuthenticationError,
    PaymentEventStatus,
    PaymentLookupError,
    PaymentOperationFacts,
    ProviderChargeStatus,
    SyntheticPixAdapter,
)
from apps.billing.models import (
    Invoice,
    PaymentEvent,
    PixCharge,
    Receipt,
    Settlement,
)
from apps.billing.pix import complete_pix_charge, prepare_pix_charge
from apps.billing.reconciliation import (
    PaymentContextError,
    clear_payment_event_adapters,
    receive_payment_event,
    reconcile_charge,
    register_payment_event_adapter,
)
from apps.billing.services import (
    BillingAccessDeniedError,
    cancel_invoice,
    create_invoice,
    issue_invoice,
    view_invoice,
)
from apps.identity.models import UserClinicRole
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from psycopg import sql

from appointment_service_support import seed_appointment_setup
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from appointment_service_support import AppointmentSetup
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
PROVIDER: Final = "synthetic-pix-v1"
SECRET: Final = "synthetic-payment-secret"  # noqa: S105 - rehearsal token
TABLES: Final = ("billing_paymentevent",)


class LookupInsideTransactionError(Exception):
    """Fail the test if the provider lookup ran inside a transaction."""


@dataclass
class ScriptedPixAdapter:
    """Synthetic-contract provider double with scripted authoritative state.

    Authentication is real HMAC verification over the raw body; the lookup
    answer is scripted per test so the reconciliation boundary can be
    exercised through settled, expired, cancelled, reversed and failed
    provider states without inventing a real provider contract.
    """

    provider: str = PROVIDER
    secret: str = SECRET
    authoritative_status: PaymentEventStatus = "settled"
    authoritative_amount_minor: int | None = None
    authoritative_currency: str = "BRL"
    lookup_errors: list[Exception | type[Exception]] = field(default_factory=list)
    lookups: list[PaymentOperationFacts] = field(default_factory=list)

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AuthenticatedPaymentEvent:
        """Verify the signature and return only verified event fields."""
        signature = headers.get("x-synthetic-pix-signature", "")
        expected = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PaymentEventAuthenticationError
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise PaymentEventAuthenticationError from error
        if type(payload) is not dict:
            raise PaymentEventAuthenticationError
        event_id = payload.get("event_id")
        provider_reference = payload.get("provider_reference")
        status = payload.get("status")
        amount_minor = payload.get("amount_minor")
        currency = payload.get("currency")
        if (
            type(event_id) is not str
            or not event_id
            or type(provider_reference) is not str
            or not provider_reference
            or status not in ("pending", "settled", "expired", "cancelled", "reversed")
            or type(amount_minor) is not int
            or amount_minor <= 0
            or type(currency) is not str
            or len(currency) != 3
        ):
            raise PaymentEventAuthenticationError
        return AuthenticatedPaymentEvent(
            event_id=event_id,
            provider_reference=provider_reference,
            status=status,
            amount_minor=amount_minor,
            currency=currency,
        )

    def lookup(self, operation: PaymentOperationFacts) -> ProviderChargeStatus:
        """Answer from scripted state; the call must see no open transaction."""
        self.lookups.append(operation)
        if connection.in_atomic_block:
            raise LookupInsideTransactionError
        if self.lookup_errors:
            raise self.lookup_errors.pop(0)
        return ProviderChargeStatus(
            status=self.authoritative_status,
            amount_minor=(
                operation.amount_minor
                if self.authoritative_amount_minor is None
                else self.authoritative_amount_minor
            ),
            currency=self.authoritative_currency,
        )


@dataclass
class HeldLookupAdapter(ScriptedPixAdapter):
    """Scripted adapter that holds chosen threads' lookups until released.

    ``holds`` maps a thread-name prefix to ``(captured, release)`` events: a
    matching lookup signals capture, then blocks until the test releases it,
    so a newer observation can commit while the older answer is in flight.
    """

    holds: dict[str, tuple[Event, Event]] = field(default_factory=dict)

    def lookup(self, operation: PaymentOperationFacts) -> ProviderChargeStatus:
        """Answer after signaling capture and waiting for the release."""
        answer = super().lookup(operation)
        for prefix, (captured, release) in self.holds.items():
            if current_thread().name.startswith(prefix):
                captured.set()
                if not release.wait(timeout=20):
                    raise TimeoutError
        return answer


@pytest.fixture(autouse=True)
def synthetic(settings: SettingsWrapper) -> None:
    settings.CLINIC_DATA_MODE = "synthetic"
    settings.BILLING_SYNTHETIC_PIX = True
    settings.BILLING_SYNTHETIC_PIX_SECRET = SECRET


@pytest.fixture
def scripted() -> Iterator[ScriptedPixAdapter]:
    adapter = ScriptedPixAdapter()
    register_payment_event_adapter(adapter)
    try:
        yield adapter
    finally:
        clear_payment_event_adapters()


@pytest.fixture
def rehearsal(settings: SettingsWrapper) -> Iterator[SyntheticPixAdapter]:
    adapter = SyntheticPixAdapter()
    register_payment_event_adapter(adapter)
    try:
        yield adapter
    finally:
        clear_payment_event_adapters()


@pytest.fixture
def charged(
    rbac_graph: RbacGraph, scripted: ScriptedPixAdapter
) -> tuple[AppointmentSetup, Invoice, PixCharge]:
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
        operation = prepare_pix_charge(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        charge = complete_pix_charge(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            operation_id=operation.pk,
        )
    return setup, invoice, charge


def _signed_event(  # noqa: PLR0913
    *,
    event_id: str,
    provider_reference: str,
    status: str = "settled",
    amount_minor: int = 12345,
    currency: str = "BRL",
    secret: str = SECRET,
    extra_claims: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], bytes]:
    payload: dict[str, object] = {
        "event_id": event_id,
        "provider_reference": provider_reference,
        "status": status,
        "amount_minor": amount_minor,
        "currency": currency,
    }
    if extra_claims:
        payload.update(extra_claims)
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-synthetic-pix-signature": signature}, body


def _deliver(
    headers: Mapping[str, str], body: bytes, *, provider: str = PROVIDER
) -> str:
    return receive_payment_event(provider=provider, headers=headers, body=body)


def _events() -> list[PaymentEvent]:
    return list(PaymentEvent.objects.order_by("received_at", "pk"))


def _audit_verbs(organization_id: UUID) -> list[str]:
    rows = AuditEvent.objects.filter(
        organization_id=organization_id,
        event_type__startswith="billing.payment.",
    ).values_list("payload__object_verb", flat=True)
    return sorted(rows)


def test_verified_settlement_creates_one_receipt_and_deduplicates(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    headers, body = _signed_event(
        event_id="evt-settle-1", provider_reference=charge.provider_reference
    )
    with runtime_role():
        assert _deliver(headers, body) == "settled"
        assert _deliver(headers, body) == "duplicate"
        other_headers, other_body = _signed_event(
            event_id="evt-settle-2",
            provider_reference=charge.provider_reference,
        )
        assert _deliver(other_headers, other_body) == "recorded"
        assert _deliver(other_headers, other_body) == "duplicate"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "paid"
        (settlement,) = Settlement.objects.all()
        (receipt,) = Receipt.objects.all()
        (first, second) = _events()
        assert receipt.settlement_id == settlement.pk
        assert settlement.invoice_id == invoice.pk
        assert settlement.currency == "BRL"
        assert settlement.amount_minor == 12345
        assert settlement.confirmed_by_id == setup.actor_id
        assert first.resolution == "settled"
        assert first.settlement_id == settlement.pk
        assert first.event_id == "evt-settle-1"
        assert first.authoritative_status == "settled"
        assert first.reason_code is None
        assert second.resolution == "recorded"
        assert second.reason_code == "already_settled"
        assert second.settlement_id is None
        assert receipt.amount_minor == 12345
        assert receipt.invoice_id == invoice.pk
    assert _audit_verbs(setup.organization_id) == ["recorded", "settled"]


def test_reconcile_recovers_missed_settlement_without_duplicates(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    with runtime_role():
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "settled"
        )
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "duplicate"
        )
        # A late real webhook for the recovered settlement records, never
        # duplicates money records.
        headers, body = _signed_event(
            event_id="evt-late-webhook",
            provider_reference=charge.provider_reference,
        )
        assert _deliver(headers, body) == "recorded"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "paid"
        assert Settlement.objects.count() == Receipt.objects.count() == 1
        assert PaymentEvent.objects.count() == 2
        assert {event.resolution for event in PaymentEvent.objects.all()} == {
            "settled",
            "recorded",
        }


def test_forged_and_unauthenticated_events_change_nothing(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    headers, body = _signed_event(
        event_id="evt-forged", provider_reference=charge.provider_reference
    )
    with runtime_role():
        with pytest.raises(PaymentEventAuthenticationError):
            _deliver({"x-synthetic-pix-signature": "forged"}, body)
        with pytest.raises(PaymentEventAuthenticationError):
            _deliver(headers, body, provider="unregistered-provider")
        with pytest.raises(PaymentEventAuthenticationError):
            _deliver(headers, body + b"tampered")
        bad = json.dumps({"event_id": "e", "status": "settled"}).encode()
        signature = hmac.new(SECRET.encode(), bad, hashlib.sha256).hexdigest()
        with pytest.raises(PaymentEventAuthenticationError):
            _deliver({"x-synthetic-pix-signature": signature}, bad)

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        assert not PaymentEvent.objects.exists()
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
    assert _audit_verbs(setup.organization_id) == []


def test_unknown_reference_and_tenant_claims_are_rejected_or_ignored(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    rbac_graph: RbacGraph,
) -> None:
    setup, invoice, charge = charged
    headers, body = _signed_event(
        event_id="evt-unknown", provider_reference="synthetic-pix-unknown"
    )
    with runtime_role():
        assert _deliver(headers, body) == "rejected"
        claimed, claimed_body = _signed_event(
            event_id="evt-claims",
            provider_reference=charge.provider_reference,
            extra_claims={
                "organization_id": str(rbac_graph.organization_b),
                "clinic_id": str(rbac_graph.clinic_c),
                "invoice_id": str(uuid4()),
            },
        )
        assert _deliver(claimed, claimed_body) == "settled"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "paid"
        (event,) = _events()
        assert event.organization_id == setup.organization_id
        assert event.invoice_id == invoice.pk
    assert _audit_verbs(rbac_graph.organization_b) == []


@pytest.mark.parametrize(
    ("status", "amount", "currency", "reason"),
    [
        ("settled", 12346, "BRL", "overpaid"),
        ("settled", 12344, "BRL", "underpaid"),
        ("settled", 12345, "USD", "currency_mismatch"),
        ("expired", 12345, "BRL", "status_mismatch"),
        ("cancelled", 12345, "BRL", "status_mismatch"),
        ("reversed", 12345, "BRL", "reversed"),
    ],
)
def test_mismatched_and_terminal_events_require_operator_action(  # noqa: PLR0913
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    scripted: ScriptedPixAdapter,
    status: str,
    amount: int,
    currency: str,
    reason: str,
) -> None:
    setup, invoice, charge = charged
    headers, body = _signed_event(
        event_id=f"evt-{reason}",
        provider_reference=charge.provider_reference,
        status=status,
        amount_minor=amount,
        currency=currency,
    )
    with runtime_role():
        assert _deliver(headers, body) == "operator_required"
        assert _deliver(headers, body) == "duplicate"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        (event,) = _events()
        assert event.resolution == "operator_required"
        assert event.reason_code == reason
        assert event.reported_status == status
        assert event.authoritative_status == "settled"
        assert event.settlement_id is None
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
    assert _audit_verbs(setup.organization_id) == ["flagged"]


@pytest.mark.parametrize(
    ("authoritative", "event_status", "reason"),
    [
        ("expired", "expired", "expired"),
        ("cancelled", "cancelled", "cancelled"),
        ("expired", "settled", "unverified_settlement"),
        ("pending", "expired", "pending"),
        ("pending", "settled", "unverified_settlement"),
        ("reversed", "reversed", "reversed"),
    ],
)
def test_authoritative_states_drive_explicit_resolutions(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    scripted: ScriptedPixAdapter,
    authoritative: PaymentEventStatus,
    event_status: str,
    reason: str,
) -> None:
    setup, invoice, charge = charged
    scripted.authoritative_status = authoritative
    headers, body = _signed_event(
        event_id=f"evt-{authoritative}-{event_status}",
        provider_reference=charge.provider_reference,
        status=event_status,
    )
    with runtime_role():
        expected = "recorded" if reason == "pending" else "operator_required"
        assert _deliver(headers, body) == expected

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        (event,) = _events()
        assert event.reason_code == reason
        assert event.authoritative_status == authoritative
        assert not Settlement.objects.exists()


def test_provider_query_failure_is_recorded_not_guessed(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    scripted: ScriptedPixAdapter,
) -> None:
    setup, invoice, charge = charged
    scripted.lookup_errors.append(PaymentLookupError)
    headers, body = _signed_event(
        event_id="evt-unverifiable",
        provider_reference=charge.provider_reference,
    )
    with runtime_role():
        assert _deliver(headers, body) == "operator_required"
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "settled"
        )

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "paid"
        assert Settlement.objects.count() == Receipt.objects.count() == 1
        first, second = _events()
        assert (first.resolution, first.reason_code) == (
            "operator_required",
            "provider_query_failed",
        )
        assert first.authoritative_status is None
        assert (second.resolution, second.reason_code) == ("settled", None)


def test_reconcile_pending_and_unknown_charge(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    scripted: ScriptedPixAdapter,
) -> None:
    setup, invoice, charge = charged
    scripted.authoritative_status = "pending"
    with runtime_role():
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "pending"
        )
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference="synthetic-pix-missing"
            )
            == "rejected"
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert not PaymentEvent.objects.exists()
        invoice.refresh_from_db()
        assert invoice.state == "open"


def test_synthetic_rehearsal_events_can_never_settle(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    rehearsal: SyntheticPixAdapter,
) -> None:
    setup, invoice, charge = charged
    register_payment_event_adapter(rehearsal)
    headers, body = _signed_event(
        event_id="evt-synthetic-settled",
        provider_reference=charge.provider_reference,
    )
    with runtime_role():
        assert _deliver(headers, body) == "operator_required"
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "pending"
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        (event,) = _events()
        assert event.reason_code == "unverified_settlement"
        assert event.authoritative_status == "pending"
        assert not Settlement.objects.exists()


def test_synthetic_authentication_fails_closed(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    rehearsal: SyntheticPixAdapter,
    settings: SettingsWrapper,
) -> None:
    setup, _invoice, charge = charged
    register_payment_event_adapter(rehearsal)
    headers, body = _signed_event(
        event_id="evt-no-secret", provider_reference=charge.provider_reference
    )
    settings.BILLING_SYNTHETIC_PIX_SECRET = None
    with runtime_role(), pytest.raises(PaymentEventAuthenticationError):
        _deliver(headers, body)
    settings.BILLING_SYNTHETIC_PIX_SECRET = SECRET
    settings.BILLING_SYNTHETIC_PIX = False
    with runtime_role(), pytest.raises(PaymentEventAuthenticationError):
        _deliver(headers, body)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert not PaymentEvent.objects.exists()


def test_revoked_actor_records_event_but_cannot_settle(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    superuser_database_url: str,
) -> None:
    setup, _invoice, charge = charged
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        UserClinicRole.objects.filter(
            user_id=setup.actor_id,
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
        ).delete()

    headers, body = _signed_event(
        event_id="evt-revoked", provider_reference=charge.provider_reference
    )
    with runtime_role():
        assert _deliver(headers, body) == "operator_required"

    # The recorded actor no longer passes the staff predicate, so the
    # retained history is verified through the RLS-bypassing superuser.
    with psycopg.connect(superuser_database_url) as raw:
        rows = raw.execute(
            "SELECT e.resolution, e.reason_code, i.state "
            "FROM clinic_app.billing_paymentevent e "
            "JOIN clinic_app.billing_invoice i ON i.id = e.invoice_id"
        ).fetchall()
        assert rows == [("operator_required", "settlement_rejected", "open")]
        counts = raw.execute(
            "SELECT count(*) FROM clinic_app.billing_settlement "
            "UNION ALL SELECT count(*) FROM clinic_app.billing_receipt"
        ).fetchall()
        assert [row[0] for row in counts] == [0, 0]
    assert _audit_verbs(setup.organization_id) == ["flagged"]


def test_cross_tenant_event_never_touches_other_tenant_invoice(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    rbac_graph: RbacGraph,
) -> None:
    setup, invoice, charge = charged
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_b)],
        )
        UserClinicRole.objects.create(
            user_id=rbac_graph.shared_user,
            organization_id=rbac_graph.organization_b,
            clinic_id=rbac_graph.clinic_c,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        other_patient = create_patient(
            clinic_id=rbac_graph.clinic_c,
            full_name="Other Tenant Patient",
            birth_date=date(1999, 5, 5),
            idempotency_key=uuid4(),
        )
        other_invoice = create_invoice(
            clinic_id=rbac_graph.clinic_c,
            patient_id=other_patient.patient.pk,
            amount_minor=777,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=rbac_graph.clinic_c,
            invoice_id=other_invoice.pk,
            expected_revision=1,
        )
        other_operation = prepare_pix_charge(
            clinic_id=rbac_graph.clinic_c, invoice_id=other_invoice.pk
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        other_charge = complete_pix_charge(
            clinic_id=rbac_graph.clinic_c,
            invoice_id=other_invoice.pk,
            operation_id=other_operation.pk,
        )

    # An event naming tenant A's charge settles tenant A only; tenant B's
    # invoice, events and audit stay untouched.
    headers, body = _signed_event(
        event_id="evt-tenant-a",
        provider_reference=charge.provider_reference,
        amount_minor=777,
    )
    with runtime_role():
        assert _deliver(headers, body) == "operator_required"
        other_headers, other_body = _signed_event(
            event_id="evt-tenant-b",
            provider_reference=other_charge.provider_reference,
            amount_minor=777,
        )
        assert _deliver(other_headers, other_body) == "settled"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        (event,) = _events()
        assert event.reason_code == "underpaid"
        assert event.invoice_id == invoice.pk
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        other_invoice.refresh_from_db()
        assert other_invoice.state == "paid"
        assert Settlement.objects.count() == Receipt.objects.count() == 1
        (other_event,) = _events()
        assert other_event.invoice_id == other_invoice.pk
        assert other_event.organization_id == rbac_graph.organization_b
    assert _audit_verbs(setup.organization_id) == ["flagged"]
    assert _audit_verbs(rbac_graph.organization_b) == ["settled"]


def test_parallel_deliveries_converge_to_one_settlement(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, _invoice, charge = charged
    headers, body = _signed_event(
        event_id="evt-race", provider_reference=charge.provider_reference
    )
    barrier = Barrier(2, timeout=15)
    outcomes: list[str] = []

    def deliver() -> None:
        try:
            with runtime_role():
                barrier.wait()
                outcomes.append(_deliver(headers, body))
        except BaseException as error:  # noqa: BLE001 - surfaced by assertion
            outcomes.append(f"error:{type(error).__name__}:{error}")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert sorted(outcomes) == ["duplicate", "settled"]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Settlement.objects.count() == Receipt.objects.count() == 1
        assert PaymentEvent.objects.count() == 1


def test_stale_lookup_after_newer_reversal_never_settles(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    lookup_captured = Event()
    release_lookup = Event()
    adapter = HeldLookupAdapter(holds={"delayed": (lookup_captured, release_lookup)})
    register_payment_event_adapter(adapter)
    old_headers, old_body = _signed_event(
        event_id="evt-old-settled", provider_reference=charge.provider_reference
    )
    new_headers, new_body = _signed_event(
        event_id="evt-new-reversed",
        provider_reference=charge.provider_reference,
        status="reversed",
    )

    def delayed_delivery() -> str:
        try:
            with runtime_role():
                return _deliver(old_headers, old_body)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed") as pool:
        old = pool.submit(delayed_delivery)
        try:
            assert lookup_captured.wait(timeout=20), "old lookup never captured"
            # The provider's authoritative state moves to reversed and the
            # newer reversal event commits while the older lookup is held.
            adapter.authoritative_status = "reversed"
            with runtime_role():
                assert _deliver(new_headers, new_body) == "operator_required"
        finally:
            release_lookup.set()
        assert old.result(timeout=30) == "operator_required"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
        reversal, stale = _events()
        assert (reversal.event_id, reversal.resolution, reversal.reason_code) == (
            "evt-new-reversed",
            "operator_required",
            "reversed",
        )
        assert (stale.event_id, stale.resolution, stale.reason_code) == (
            "evt-old-settled",
            "operator_required",
            "superseded",
        )
        assert stale.authoritative_status == "settled"
    assert _audit_verbs(setup.organization_id) == ["flagged", "flagged"]


def test_stale_lookup_after_newer_overpayment_never_settles(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    lookup_captured = Event()
    release_lookup = Event()
    adapter = HeldLookupAdapter(holds={"delayed": (lookup_captured, release_lookup)})
    register_payment_event_adapter(adapter)
    old_headers, old_body = _signed_event(
        event_id="evt-old-exact", provider_reference=charge.provider_reference
    )
    new_headers, new_body = _signed_event(
        event_id="evt-new-overpaid",
        provider_reference=charge.provider_reference,
        amount_minor=12346,
    )

    def delayed_delivery() -> str:
        try:
            with runtime_role():
                return _deliver(old_headers, old_body)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed") as pool:
        old = pool.submit(delayed_delivery)
        try:
            assert lookup_captured.wait(timeout=20), "old lookup never captured"
            # The provider's authoritative amount moves to 12346 and the
            # newer overpaid observation commits while the older exact
            # settled lookup is held.
            adapter.authoritative_amount_minor = 12346
            with runtime_role():
                assert _deliver(new_headers, new_body) == "operator_required"
        finally:
            release_lookup.set()
        assert old.result(timeout=30) == "operator_required"

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "open"
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
        overpaid, stale = _events()
        assert (overpaid.event_id, overpaid.resolution, overpaid.reason_code) == (
            "evt-new-overpaid",
            "operator_required",
            "overpaid",
        )
        assert overpaid.authoritative_amount_minor == 12346
        assert (stale.event_id, stale.resolution, stale.reason_code) == (
            "evt-old-exact",
            "operator_required",
            "superseded",
        )
        assert stale.authoritative_status == "settled"
        assert stale.authoritative_amount_minor == 12345
    assert _audit_verbs(setup.organization_id) == ["flagged", "flagged"]


def test_superseded_recovery_still_recovers_missed_settlement(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    pending_captured, release_pending = Event(), Event()
    settled_captured, release_settled = Event(), Event()
    adapter = HeldLookupAdapter(
        authoritative_status="pending",
        holds={
            "pending-delivery": (pending_captured, release_pending),
            "recovery": (settled_captured, release_settled),
        },
    )
    register_payment_event_adapter(adapter)
    pending_headers, pending_body = _signed_event(
        event_id="evt-old-pending",
        provider_reference=charge.provider_reference,
        status="pending",
    )

    def pending_delivery() -> str:
        try:
            with runtime_role():
                return _deliver(pending_headers, pending_body)
        finally:
            connections.close_all()

    def recover() -> str:
        try:
            with runtime_role():
                return reconcile_charge(
                    provider=PROVIDER,
                    provider_reference=charge.provider_reference,
                )
        finally:
            connections.close_all()

    with (
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pending-delivery"
        ) as pending_pool,
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="recovery"
        ) as recovery_pool,
    ):
        pending = pending_pool.submit(pending_delivery)
        try:
            assert pending_captured.wait(timeout=20), "pending never captured"
            # The provider settles while the older pending lookup is held;
            # the recovery lookup captures settled, then the older pending
            # observation commits first and supersedes it.
            adapter.authoritative_status = "settled"
            recovery = recovery_pool.submit(recover)
            assert settled_captured.wait(timeout=20), "settled never captured"
            release_pending.set()
            assert pending.result(timeout=30) == "recorded"
        finally:
            release_pending.set()
            release_settled.set()
        assert recovery.result(timeout=30) == "operator_required"

    # The superseded attempt must not consume the recovered facts: a fresh
    # authoritative settled lookup settles exactly once, and a later retry
    # deduplicates on the recorded terms instead of minting money records.
    with runtime_role():
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "settled"
        )
        assert (
            reconcile_charge(
                provider=PROVIDER, provider_reference=charge.provider_reference
            )
            == "duplicate"
        )

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice.refresh_from_db()
        assert invoice.state == "paid"
        assert Settlement.objects.count() == Receipt.objects.count() == 1
        pending_row, superseded_row, settled_row = _events()
        assert (pending_row.event_id, pending_row.resolution) == (
            "evt-old-pending",
            "recorded",
        )
        assert pending_row.reason_code == "pending"
        assert superseded_row.event_id.startswith(
            f"reconcile:{charge.provider_reference}:settled:12345:BRL"
        )
        assert (superseded_row.resolution, superseded_row.reason_code) == (
            "operator_required",
            "superseded",
        )
        assert settled_row.event_id.startswith(
            f"reconcile:{charge.provider_reference}:settled:12345:BRL"
        )
        assert settled_row.event_id != superseded_row.event_id
        assert (settled_row.resolution, settled_row.reason_code) == (
            "settled",
            None,
        )
        assert settled_row.settlement_id is not None
    assert _audit_verbs(setup.organization_id) == [
        "flagged",
        "recorded",
        "settled",
    ]


def test_events_for_cancelled_or_settled_invoice_never_mutate_terms(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, invoice, charge = charged
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        cancel_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
    headers, body = _signed_event(
        event_id="evt-after-cancel",
        provider_reference=charge.provider_reference,
    )
    with runtime_role():
        assert _deliver(headers, body) == "operator_required"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        (event,) = _events()
        assert event.reason_code == "invoice_not_open"
        invoice.refresh_from_db()
        assert invoice.state == "cancelled"
        assert not Settlement.objects.exists()


def test_event_history_is_append_only_and_clinic_scoped(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
    rbac_graph: RbacGraph,
) -> None:
    setup, invoice, charge = charged
    headers, body = _signed_event(
        event_id="evt-history", provider_reference=charge.provider_reference
    )
    with runtime_role():
        assert _deliver(headers, body) == "settled"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        (event,) = _events()
        with pytest.raises(DatabaseError), transaction.atomic():
            PaymentEvent.objects.all().update(reason_code="tampered")
        with pytest.raises(DatabaseError), transaction.atomic():
            PaymentEvent.objects.all().delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            PaymentEvent.objects.create(
                organization_id=setup.organization_id,
                operation_id=event.operation_id,
                invoice_id=invoice.pk,
                provider=PROVIDER,
                event_id="evt-fabricated",
                provider_reference=charge.provider_reference,
                actor_id=setup.actor_id,
                reported_status="settled",
                reported_amount_minor=1,
                reported_currency="BRL",
                authoritative_status="settled",
                authoritative_amount_minor=1,
                authoritative_currency="BRL",
                resolution="settled",
                settlement_id=event.settlement_id,
            )
        event.refresh_from_db()
        assert event.resolution == "settled"
    for actor, organization in [
        (rbac_graph.clinic_admin, rbac_graph.organization_a),
        (rbac_graph.physician, rbac_graph.organization_a),
        (rbac_graph.shared_user, rbac_graph.organization_b),
    ]:
        with runtime_role(), tenant_context(actor, organization):
            assert not PaymentEvent.objects.exists()
            with pytest.raises(BillingAccessDeniedError):
                view_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
    with runtime_role():
        assert not PaymentEvent.objects.exists()


def test_receive_inside_transaction_is_rejected(
    charged: tuple[AppointmentSetup, Invoice, PixCharge],
) -> None:
    setup, _invoice, charge = charged
    headers, body = _signed_event(
        event_id="evt-nested", provider_reference=charge.provider_reference
    )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(PaymentContextError):
            _deliver(headers, body)
        assert not PaymentEvent.objects.exists()


def test_exact_event_table_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, "
            "relowner::regrole::text FROM pg_class "
            "WHERE relnamespace = 'clinic_app'::regnamespace AND relname = ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename, policyname, cmd FROM pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("billing_paymentevent", "billing_staff", "SELECT"),
            ("billing_paymentevent", "billing_event_recorder", "INSERT"),
        }
        cursor.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.table_privileges "
            "WHERE table_schema = 'clinic_app' AND table_name = ANY(%s) "
            "AND grantee = 'clinic_app'",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, privilege) for table in TABLES for privilege in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT has_function_privilege('clinic_app', "
            "'clinic_app.billing_payment_event_guard()', 'EXECUTE')"
        )
        assert cursor.fetchone() == (False,)
    with runtime_role(), connection.cursor() as cursor:
        for table in TABLES:
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    sql.SQL("DELETE FROM clinic_app.{}").format(sql.Identifier(table))
                )
