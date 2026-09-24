"""Payment screens over real HTTP, real RLS and the real patient session.

Every assertion here runs against PostgreSQL with the runtime role: staff
authority, patient-session scope and the database's own charge history decide
what each screen may render. The screens are exercised through their real
routes, so an idempotent retry, an expired code, an unverified payment event
and another patient's link are proven, not described.
"""

from __future__ import annotations

import base64
import re
from contextlib import contextmanager
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.billing.models import (
    Invoice,
    PaymentEvent,
    PixCharge,
    PixOperation,
    Receipt,
    Settlement,
)
from apps.billing.services import (
    create_invoice,
    issue_invoice,
    release_invoice,
)
from apps.identity.models import User, UserClinicRole
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    issue_invitation,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.test import Client
from django.utils import timezone

from auth.stepup_test_support import create_role_actor
from otp_test_support import create_totp_device, fixed_otp_time, token_for
from otp_test_support import runtime_role as http_runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

OK: Final = 200
FOUND: Final = 302
FORBIDDEN: Final = 403
CONFLICT: Final = 409
INVALID: Final = 400
AMOUNT_MINOR: Final = 18000
AMOUNT_TEXT: Final = "180,00"
POLL_LIMIT: Final = 15
QR_PATTERN: Final = re.compile(r'src="data:image/png;base64,([^"]+)"')
REPEAT_PATTERN: Final = re.compile(r'name="repeat_of"[^>]*value="([^"]+)"')
AMOUNT_PATTERN: Final = re.compile(r'name="amount"[^>]*value="([^"]+)"')
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def _synthetic_pix(settings: pytest.FixtureRequest) -> None:
    """Open only the explicitly synthetic, non-payable rehearsal gate."""
    settings.BILLING_SYNTHETIC_PIX = True  # type: ignore[attr-defined]


@contextmanager
def staff_client(user_id: UUID) -> Iterator[Client]:
    """Sign in one staff user through the real login and TOTP verify flow."""
    user = User.objects.get(pk=user_id)
    device = create_totp_device(user_id, confirmed=True)
    client = Client()
    with http_runtime_role(), fixed_otp_time():
        assert (
            client.post(
                "/auth/login/",
                {"username": user.username, "password": RBAC_RAW_CREDENTIAL},
            ).status_code
            == FOUND
        )
        assert (
            client.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            ).status_code
            == FOUND
        )
        yield client


def patient_client(session_id: UUID) -> Client:
    """Bind one signed cookie to a real patient session, as the portal does."""
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(session_id)
    session.save()
    return client


class Scenario:
    """One clinic admin, one enrolled patient and their charge routes."""

    def __init__(self, graph: RbacGraph) -> None:
        """Seed the manager and the enrolled synthetic patient."""
        self.graph = graph
        self.manager = create_role_actor(graph, UserClinicRole.Role.CLINIC_ADMIN)
        with self.scope():
            registration = create_patient(
                clinic_id=graph.clinic_a,
                full_name="Paciente Sintetico Cobranca",
                birth_date=date(1990, 5, 17),
                idempotency_key=uuid4(),
            )
        self.patient_id = registration.patient.pk
        self.enrollment_id = registration.enrollment.pk

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Read stored rows exactly as the runtime role and tenant see them."""
        with (
            http_runtime_role(),
            tenant_context(self.manager.pk, self.graph.organization_a),
        ):
            yield

    @property
    def charges_url(self) -> str:
        """Return the staff charge ledger of this clinic."""
        return f"/billing/clinics/{self.graph.clinic_a}/charges/"

    def invoice_url(self, invoice_id: UUID) -> str:
        """Return the staff screen of one charge."""
        return f"{self.charges_url}{invoice_id}/"

    def status_url(self, invoice_id: UUID) -> str:
        """Return the bounded staff refresh endpoint of one charge."""
        return f"{self.charges_url}{invoice_id}/status/"

    def issued_charge(self, *, released: bool = False) -> Invoice:
        """Create one issued charge through the real services."""
        with self.scope():
            invoice = create_invoice(
                clinic_id=self.graph.clinic_a,
                patient_id=self.patient_id,
                amount_minor=AMOUNT_MINOR,
                idempotency_key=uuid4(),
            )
            issued = issue_invoice(
                clinic_id=self.graph.clinic_a,
                invoice_id=invoice.pk,
                expected_revision=1,
            )
            if released:
                release_invoice(clinic_id=self.graph.clinic_a, invoice_id=invoice.pk)
            return issued

    def session_for(self, enrollment_id: UUID | None = None) -> UUID:
        """Redeem one real invitation and return its live session id."""
        with self.scope():
            invitation = issue_invitation(
                clinic_id=self.graph.clinic_a,
                enrollment_id=enrollment_id or self.enrollment_id,
            )
        with http_runtime_role():
            session = redeem_invitation(self.graph.clinic_a, invitation.secret)
        assert session is not None
        return session


def _post(client: Client, url: str, fields: dict[str, str]) -> int:
    with http_runtime_role():
        return int(client.post(url, fields).status_code)


def _get(client: Client, url: str) -> tuple[int, str]:
    with http_runtime_role():
        response = client.get(url)
    return int(response.status_code), response.content.decode()


def _create_fields(
    client: Client, scenario: Scenario, amount: str = AMOUNT_TEXT
) -> dict[str, str]:
    """Return exactly what one freshly rendered create form submits."""
    status, page = _get(client, scenario.charges_url)
    assert status == OK
    # A fresh form repeats nothing and mints no per-page operation name.
    assert REPEAT_PATTERN.search(page) is None
    return {
        "action": "create",
        "patient_id": str(scenario.patient_id),
        "amount": amount,
    }


def _repeat_fields(
    client: Client, scenario: Scenario, invoice_id: UUID
) -> dict[str, str]:
    """Return what the deliberate repeat form submits, as the server filled it."""
    status, page = _get(client, f"{scenario.charges_url}?repeat_of={invoice_id}")
    assert status == OK
    repeated = REPEAT_PATTERN.search(page)
    prefilled = AMOUNT_PATTERN.search(page)
    assert repeated is not None
    assert prefilled is not None
    assert repeated.group(1) == str(invoice_id)
    assert prefilled.group(1) == AMOUNT_TEXT
    return {
        "action": "create",
        "patient_id": str(scenario.patient_id),
        "amount": prefilled.group(1),
        "repeat_of": repeated.group(1),
    }


def _expired_code(scenario: Scenario, invoice: Invoice) -> PixOperation:
    """Store one already expired request with its exact synthetic result."""
    now = timezone.now()
    with scenario.scope():
        operation = PixOperation.objects.create(
            organization_id=invoice.organization_id,
            invoice=invoice,
            invoice_reference=invoice.reference,
            amount_minor=invoice.amount_minor,
            currency=invoice.currency,
            actor_id=scenario.manager.pk,
            created_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        PixCharge.objects.create(
            organization_id=invoice.organization_id,
            operation=operation,
            invoice=invoice,
            provider_reference=f"synthetic-pix-{operation.pk}",
            copy_code=f"SYNTHETIC-NOT-PAYABLE|synthetic-pix-{operation.pk}|expirado",
            qr_base64=base64.b64encode(PNG_SIGNATURE).decode("ascii"),
        )
        return operation


def test_staff_journey_creates_one_charge_and_one_receipt(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    confirmation = uuid4()
    with staff_client(scenario.manager.pk) as client:
        assert (
            _post(client, scenario.charges_url, _create_fields(client, scenario))
            == FOUND
        )
        with scenario.scope():
            invoice = Invoice.objects.get()
        assert invoice.amount_minor == AMOUNT_MINOR
        status, page = _get(client, scenario.invoice_url(invoice.pk))
        assert status == OK
        assert 'data-payment-state="draft"' in page
        assert "R$ 180,00" in page

        assert (
            _post(
                client,
                scenario.invoice_url(invoice.pk),
                {"action": "issue", "expected_revision": "1"},
            )
            == FOUND
        )
        status, page = _get(client, scenario.invoice_url(invoice.pk))
        assert 'data-payment-state="issued"' in page

        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "code"}) == FOUND
        )
        status, page = _get(client, scenario.invoice_url(invoice.pk))
        assert 'data-payment-state="pending"' in page
        assert "SYNTHETIC-NOT-PAYABLE" in page
        image = QR_PATTERN.search(page)
        assert image is not None
        assert base64.b64decode(image.group(1)).startswith(PNG_SIGNATURE)
        # Creating a charge never claims payment.
        with scenario.scope():
            assert not Receipt.objects.exists()

        # A retried submit converges on the one stored request.
        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "code"}) == FOUND
        )
        with scenario.scope():
            assert PixOperation.objects.count() == 1
            assert PixCharge.objects.count() == 1

        assert (
            _post(
                client,
                scenario.invoice_url(invoice.pk),
                {
                    "action": "confirm",
                    "confirmation_reference": str(confirmation),
                    "amount": AMOUNT_TEXT,
                    "attested": "on",
                },
            )
            == FOUND
        )
        status, page = _get(client, scenario.invoice_url(invoice.pk))
        with scenario.scope():
            receipt = Receipt.objects.get()
        assert 'data-payment-state="paid"' in page
        assert str(receipt.reference) in page
        assert "SYNTHETIC-NOT-PAYABLE" not in page

        # The same attested confirmation returns the same receipt.
        assert (
            _post(
                client,
                scenario.invoice_url(invoice.pk),
                {
                    "action": "confirm",
                    "confirmation_reference": str(confirmation),
                    "amount": AMOUNT_TEXT,
                    "attested": "on",
                },
            )
            == FOUND
        )
        with scenario.scope():
            assert Settlement.objects.count() == 1
            assert Receipt.objects.get().pk == receipt.pk


def test_replayed_create_form_never_opens_a_second_charge(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    with staff_client(scenario.manager.pk) as client:
        submitted = _create_fields(client, scenario)
        with http_runtime_role():
            created = client.post(scenario.charges_url, submitted)
            replayed = client.post(scenario.charges_url, submitted)
        assert (created.status_code, replayed.status_code) == (FOUND, FOUND)
        with scenario.scope():
            invoice = Invoice.objects.get()
        # Both submissions resolve to the one charge the first one opened.
        assert created.headers["Location"] == scenario.invoice_url(invoice.pk)
        assert replayed.headers["Location"] == created.headers["Location"]

        # What a browser restores on Back: the same values on a page the
        # server rendered again. It resolves to the charge already opened.
        with http_runtime_role():
            restored = client.post(
                scenario.charges_url, _create_fields(client, scenario)
            )
        assert restored.status_code == FOUND
        assert restored.headers["Location"] == created.headers["Location"]
        with scenario.scope():
            assert Invoice.objects.count() == 1
            assert Invoice.objects.get().amount_minor == AMOUNT_MINOR

        # Editing the money names a different charge, opened exactly once.
        edited = {**submitted, "amount": "90,00"}
        with http_runtime_role():
            changed = client.post(scenario.charges_url, edited)
            changed_again = client.post(scenario.charges_url, edited)
        assert changed.headers["Location"] == changed_again.headers["Location"]
        assert changed.headers["Location"] != created.headers["Location"]

        # A second identical charge is deliberate, and that repeat replays too.
        repeat = _repeat_fields(client, scenario, invoice.pk)
        with http_runtime_role():
            repeated = client.post(scenario.charges_url, repeat)
            repeated_again = client.post(scenario.charges_url, repeat)
        assert repeated.headers["Location"] not in (
            created.headers["Location"],
            changed.headers["Location"],
        )
        assert repeated_again.headers["Location"] == repeated.headers["Location"]
        with scenario.scope():
            amounts = sorted(Invoice.objects.values_list("amount_minor", flat=True))
            assert amounts == [9000, AMOUNT_MINOR, AMOUNT_MINOR]

    # Another signed-in session names its own operation, never a replay.
    with staff_client(scenario.manager.pk) as other:
        assert (
            _post(other, scenario.charges_url, _create_fields(other, scenario)) == FOUND
        )
    with scenario.scope():
        assert Invoice.objects.count() == 4
        assert len(set(Invoice.objects.values_list("idempotency_key", flat=True))) == 4


def test_cancelled_charge_stops_offering_payment_instructions(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    invoice = scenario.issued_charge(released=True)
    patient = patient_client(scenario.session_for())
    with staff_client(scenario.manager.pk) as client:
        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "code"}) == FOUND
        )
        _status, payable = _get(patient, f"/patient/charges/{invoice.pk}/")
        assert "SYNTHETIC-NOT-PAYABLE" in payable

        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "cancel"})
            == FOUND
        )
        status, staff_page = _get(client, scenario.invoice_url(invoice.pk))
    assert status == OK
    assert 'data-payment-state="cancelled"' in staff_page
    assert "data-code" not in staff_page
    assert "data-qr" not in staff_page

    status, patient_page = _get(patient, f"/patient/charges/{invoice.pk}/")
    assert status == OK
    assert 'data-payment-state="cancelled"' in patient_page
    assert "data-code" not in patient_page
    assert "data-qr" not in patient_page
    assert "SYNTHETIC-NOT-PAYABLE" not in patient_page

    status, ledger = _get(patient, "/patient/charges/")
    assert status == OK
    assert 'data-state="cancelled"' in ledger
    with scenario.scope():
        # The issued code stays in history; only the screens stop offering it.
        assert PixCharge.objects.count() == 1


def test_refused_actions_keep_the_charge_and_never_claim_payment(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    invoice = scenario.issued_charge()
    with staff_client(scenario.manager.pk) as client:
        # A different amount cannot settle the frozen terms.
        assert (
            _post(
                client,
                scenario.invoice_url(invoice.pk),
                {
                    "action": "confirm",
                    "confirmation_reference": str(uuid4()),
                    "amount": "10,00",
                    "attested": "on",
                },
            )
            == CONFLICT
        )
        # An unattested confirmation is refused before any service call.
        response_status = _post(
            client,
            scenario.invoice_url(invoice.pk),
            {
                "action": "confirm",
                "confirmation_reference": str(uuid4()),
                "amount": AMOUNT_TEXT,
            },
        )
        assert response_status == INVALID
        # An unknown action is refused outright.
        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "settle"})
            == FORBIDDEN
        )
    with scenario.scope():
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
        assert Invoice.objects.get(pk=invoice.pk).state == Invoice.State.OPEN


def test_expired_code_is_refused_and_regenerates_one_successor(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    invoice = scenario.issued_charge()
    expired = _expired_code(scenario, invoice)
    with staff_client(scenario.manager.pk) as client:
        status, page = _get(client, scenario.invoice_url(invoice.pk))
        assert status == OK
        assert 'data-payment-state="expired"' in page
        assert "SYNTHETIC-NOT-PAYABLE" not in page
        assert "expirou" in page

        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "regenerate"})
            == FOUND
        )
        status, page = _get(client, scenario.invoice_url(invoice.pk))
        assert 'data-payment-state="pending"' in page
    with scenario.scope():
        successors = PixOperation.objects.exclude(pk=expired.pk)
        assert successors.count() == 1
        assert successors.get().previous_id == expired.pk


def test_unverified_payment_event_shows_verification_never_paid(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    invoice = scenario.issued_charge(released=True)
    operation = _expired_code(scenario, invoice)
    with scenario.scope():
        PaymentEvent.objects.create(
            organization_id=invoice.organization_id,
            operation=operation,
            invoice=invoice,
            provider="synthetic-pix-v1",
            event_id=f"synthetic-{uuid4().hex}",
            provider_reference=f"synthetic-pix-{operation.pk}",
            actor_id=scenario.manager.pk,
            reported_status=PaymentEvent.ReportedStatus.SETTLED,
            reported_amount_minor=invoice.amount_minor,
            reported_currency=invoice.currency,
            authoritative_status=PaymentEvent.ReportedStatus.PENDING,
            authoritative_amount_minor=invoice.amount_minor,
            authoritative_currency=invoice.currency,
            resolution=PaymentEvent.Resolution.OPERATOR_REQUIRED,
            reason_code="unverified_settlement",
        )
    with staff_client(scenario.manager.pk) as client:
        _status, page = _get(client, scenario.invoice_url(invoice.pk))
    assert 'data-payment-state="flagged"' in page
    assert "confer" in page
    patient = patient_client(scenario.session_for())
    _status, patient_page = _get(patient, f"/patient/charges/{invoice.pk}/")
    assert 'data-payment-state="flagged"' in patient_page
    assert "Pago" not in patient_page
    with scenario.scope():
        assert not Receipt.objects.exists()


def test_patient_sees_own_instructions_and_receipt_only(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    invoice = scenario.issued_charge(released=True)
    with staff_client(scenario.manager.pk) as client:
        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "code"}) == FOUND
        )
    patient = patient_client(scenario.session_for())
    status, ledger = _get(patient, "/patient/charges/")
    assert status == OK
    assert str(invoice.pk) in ledger
    assert "R$ 180,00" in ledger

    status, page = _get(patient, f"/patient/charges/{invoice.pk}/")
    assert status == OK
    assert 'data-payment-state="pending"' in page
    assert "SYNTHETIC-NOT-PAYABLE" in page
    # Re-reading the payment instructions never creates a second request.
    with scenario.scope():
        assert PixOperation.objects.count() == 1

    with staff_client(scenario.manager.pk) as client:
        assert (
            _post(
                client,
                scenario.invoice_url(invoice.pk),
                {
                    "action": "confirm",
                    "confirmation_reference": str(uuid4()),
                    "amount": AMOUNT_TEXT,
                    "attested": "on",
                },
            )
            == FOUND
        )
    status, page = _get(patient, f"/patient/charges/{invoice.pk}/")
    assert status == OK
    assert 'data-payment-state="paid"' in page
    with scenario.scope():
        receipt_reference = Receipt.objects.get().reference
    assert str(receipt_reference) in page
    assert "SYNTHETIC-NOT-PAYABLE" not in page


def test_another_patients_charge_and_unreleased_charge_stay_hidden(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    released = scenario.issued_charge(released=True)
    unreleased = scenario.issued_charge()
    with scenario.scope():
        other = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Outra Paciente Sintetica",
            birth_date=date(1988, 2, 2),
            idempotency_key=uuid4(),
        )
    other_session = scenario.session_for(other.enrollment.pk)
    stranger = patient_client(other_session)

    status, ledger = _get(stranger, "/patient/charges/")
    assert status == OK
    assert str(released.pk) not in ledger

    for invoice_id in (released.pk, unreleased.pk):
        status, page = _get(stranger, f"/patient/charges/{invoice_id}/")
        assert status == FORBIDDEN
        assert "R$" not in page
        assert str(invoice_id) not in page

    owner = patient_client(scenario.session_for())
    status, _page = _get(owner, f"/patient/charges/{unreleased.pk}/")
    assert status == FORBIDDEN


def test_status_refresh_is_bounded_and_reads_stored_state(
    rbac_graph: RbacGraph,
) -> None:
    scenario = Scenario(rbac_graph)
    invoice = scenario.issued_charge(released=True)
    with staff_client(scenario.manager.pk) as client:
        assert (
            _post(client, scenario.invoice_url(invoice.pk), {"action": "code"}) == FOUND
        )
        status, first = _get(client, f"{scenario.status_url(invoice.pk)}?attempt=0")
        assert status == OK
        assert 'hx-get="' in first
        assert "attempt=1" in first

        status, last = _get(
            client, f"{scenario.status_url(invoice.pk)}?attempt={POLL_LIMIT}"
        )
        assert status == OK
        assert "hx-get" not in last
        assert "interrompida" in last
        # The manual refresh survives the bound and never posts.
        assert scenario.invoice_url(invoice.pk) in last

    patient = patient_client(scenario.session_for())
    status, patient_status = _get(
        patient, f"/patient/charges/{invoice.pk}/status/?attempt=0"
    )
    assert status == OK
    assert "attempt=1" in patient_status
    assert 'data-payment-state="pending"' in patient_status
    with scenario.scope():
        assert PixOperation.objects.count() == 1
