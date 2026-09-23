"""Task 37 acceptance through real services, PostgreSQL RLS and runtime grants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import Invoice, InvoiceRevision, Receipt, Settlement
from apps.billing.services import (
    BillingAccessDeniedError,
    BillingConflictError,
    BillingIdempotencyConflictError,
    BillingValueError,
    cancel_invoice,
    confirm_settlement,
    create_invoice,
    issue_invoice,
    list_invoices,
    patient_charges,
    release_invoice,
    revise_invoice,
    view_invoice,
)
from apps.ehr.services import open_encounter
from apps.identity.models import UserClinicRole
from apps.intake.models import PatientAccessGrant, PatientSession
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
    revoke_patient_access,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone
from psycopg import sql

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from appointment_service_support import AppointmentSetup
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
TABLES = {
    f"billing_{name}"
    for name in ("invoice", "invoicerevision", "settlement", "receipt")
}


@contextmanager
def fixture_scope(organization_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(organization_id)]
        )
        yield


def charge(setup: AppointmentSetup, *, issued: bool = False) -> Invoice:
    invoice = create_invoice(
        clinic_id=setup.clinic_id,
        patient_id=setup.patient_id,
        amount_minor=12345,
        idempotency_key=uuid4(),
    )
    if issued:
        return issue_invoice(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=1,
        )
    return invoice


def test_exact_revisions_issue_settlement_and_receipt_lineage(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    with runtime_role(), tenant_context(setup.practitioner_id, setup.organization_id):
        encounter = open_encounter(
            clinic_id=setup.clinic_id, appointment_id=appointment.pk
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.patient_id,
            amount_minor=12345,
            idempotency_key=uuid4(),
            appointment_id=appointment.pk,
            encounter_id=encounter.pk,
        )
        revised = revise_invoice(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=1,
            amount_minor=19999,
        )
        assert revised.revision == 2
        with pytest.raises(BillingConflictError):
            issue_invoice(
                clinic_id=setup.clinic_id, invoice_id=invoice.pk, expected_revision=1
            )
        issued = issue_invoice(
            clinic_id=setup.clinic_id, invoice_id=invoice.pk, expected_revision=2
        )
        assert issued.state == "open"
        assert issued.issued_at is not None
        assert issued.released_at is None
        assert not Receipt.objects.exists()
        assert list(
            InvoiceRevision.objects.order_by("revision").values_list(
                "revision", "amount_minor", "currency", "reference", "actor_id"
            )
        ) == [
            (1, 12345, "BRL", invoice.reference, setup.actor_id),
            (2, 19999, "BRL", invoice.reference, setup.actor_id),
        ]
        confirmation = uuid4()
        receipt = confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            confirmation_reference=confirmation,
            amount_minor=19999,
        )
        repeated = confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            confirmation_reference=confirmation,
            amount_minor=19999,
        )
        assert repeated.pk == receipt.pk
        assert receipt.amount_minor == receipt.settlement.amount_minor == 19999
        assert receipt.currency == receipt.settlement.currency == "BRL"
        assert receipt.invoice_id == receipt.settlement.invoice_id == invoice.pk
        assert receipt.issued_at == receipt.settlement.confirmed_at
        assert receipt.settlement.confirmed_by_id == setup.actor_id
        assert (
            view_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk).state
            == "paid"
        )
        assert Receipt.objects.count() == Settlement.objects.count() == 1
        with pytest.raises(BillingConflictError):
            cancel_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
        with pytest.raises(BillingConflictError):
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=uuid4(),
                amount_minor=19999,
            )


@pytest.mark.parametrize(
    "amount", [-1, 0, True, 12.345, Decimal("12.345"), "123", 2**63]
)
def test_inexact_nonpositive_or_overflow_input_is_rejected(
    rbac_graph: RbacGraph, amount: int
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = charge(setup, issued=True)
        with pytest.raises(BillingValueError):
            create_invoice(
                clinic_id=setup.clinic_id,
                patient_id=setup.patient_id,
                amount_minor=amount,
                idempotency_key=uuid4(),
            )
        with pytest.raises(BillingValueError):
            revise_invoice(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                expected_revision=1,
                amount_minor=amount,
            )
        with pytest.raises(BillingValueError):
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=uuid4(),
                amount_minor=amount,
            )
        assert Invoice.objects.count() == 1
        assert not Settlement.objects.exists()


def test_replayed_create_key_returns_one_charge_and_refuses_other_terms(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.patient_id,
            amount_minor=12345,
            idempotency_key=key,
        )
        replay = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.patient_id,
            amount_minor=12345,
            idempotency_key=key,
        )
        assert replay.pk == invoice.pk
        # The replay read the stored charge; it wrote no second draft revision.
        assert InvoiceRevision.objects.filter(invoice=invoice).count() == 1

        # One key cannot silently stand for different money.
        with pytest.raises(BillingIdempotencyConflictError):
            create_invoice(
                clinic_id=setup.clinic_id,
                patient_id=setup.patient_id,
                amount_minor=999,
                idempotency_key=key,
            )

        # Intentionally identical charges stay possible under their own keys.
        second = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.patient_id,
            amount_minor=12345,
            idempotency_key=uuid4(),
        )
        assert second.pk != invoice.pk
        assert Invoice.objects.count() == 2


def test_wrong_currency_stale_edit_mismatch_and_cancel_preserve_history(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = charge(setup)
        with pytest.raises(BillingValueError):
            revise_invoice(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                expected_revision=1,
                amount_minor=100,
                currency="USD",
            )
        with pytest.raises(BillingConflictError):
            release_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
        with pytest.raises(BillingConflictError):
            revise_invoice(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                expected_revision=2,
                amount_minor=999,
            )
        issue_invoice(
            clinic_id=setup.clinic_id, invoice_id=invoice.pk, expected_revision=1
        )
        with pytest.raises(BillingConflictError):
            revise_invoice(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                expected_revision=1,
                amount_minor=999,
            )
        with pytest.raises(BillingValueError):
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=uuid4(),
                amount_minor=12345,
                currency="USD",
            )
        with pytest.raises(BillingConflictError):
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=uuid4(),
                amount_minor=12346,
            )
        cancelled = cancel_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
        assert cancelled.state == "cancelled"
        with pytest.raises(BillingConflictError):
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=uuid4(),
                amount_minor=12345,
            )
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
        assert list(InvoiceRevision.objects.values_list("amount_minor", flat=True)) == [
            12345
        ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_minor", 1),
        ("currency", "USD"),
        ("reference", uuid4()),
        ("revision", 99),
        ("state", "paid"),
        ("issued_at", None),
        ("patient_id", uuid4()),
        ("clinic_id", uuid4()),
    ],
)
def test_raw_issued_mutations_are_denied(
    rbac_graph: RbacGraph, field: str, value: object
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = charge(setup, issued=True)
        with pytest.raises(DatabaseError), transaction.atomic():
            Invoice.objects.filter(pk=invoice.pk).update(**{field: value})
        invoice.refresh_from_db()
        assert (invoice.state, invoice.amount_minor, invoice.currency) == (
            "open",
            12345,
            "BRL",
        )
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()


def test_raw_history_writes_and_fabricated_receipt_are_denied(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = charge(setup, issued=True)
        receipt = confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            confirmation_reference=uuid4(),
            amount_minor=12345,
        )
        for model in (InvoiceRevision, Settlement, Receipt):
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.all().update(amount_minor=1)
        for table in TABLES:
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    sql.SQL("DELETE FROM clinic_app.{}").format(sql.Identifier(table))
                )
        with pytest.raises(DatabaseError), transaction.atomic():
            Receipt.objects.create(
                organization_id=setup.organization_id,
                invoice=invoice,
                settlement_id=receipt.settlement_id,
                amount_minor=1,
                currency="BRL",
            )
        receipt.refresh_from_db()
        assert receipt.amount_minor == 12345


def test_database_validates_optional_sources_and_enrollment(
    rbac_graph: RbacGraph,
) -> None:
    first, second = seed_cross_clinic_appointment_setups(rbac_graph)
    with runtime_role(), tenant_context(first.actor_id, first.organization_id):
        appointment = create_synthetic_appointment(second)
        for patient, booking, encounter in (
            (first.patient_id, appointment.pk, None),
            (first.patient_id, None, uuid4()),
            (uuid4(), None, None),
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                create_invoice(
                    clinic_id=first.clinic_id,
                    patient_id=patient,
                    appointment_id=booking,
                    encounter_id=encounter,
                    amount_minor=100,
                    idempotency_key=uuid4(),
                )
        assert not Invoice.objects.exists()


def test_clinic_tenant_and_role_boundaries_apply_to_services_and_raw_reads(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = charge(setup, issued=True)
        confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            confirmation_reference=uuid4(),
            amount_minor=12345,
        )
        with pytest.raises(BillingAccessDeniedError):
            create_invoice(
                clinic_id=rbac_graph.clinic_b,
                patient_id=setup.patient_id,
                amount_minor=100,
                idempotency_key=uuid4(),
            )
    for actor, organization in [
        (rbac_graph.clinic_admin, rbac_graph.organization_a),
        (rbac_graph.physician, rbac_graph.organization_a),
        (rbac_graph.shared_user, rbac_graph.organization_b),
    ]:
        with runtime_role(), tenant_context(actor, organization):
            for model in (Invoice, InvoiceRevision, Settlement, Receipt):
                assert not model.objects.exists()
            with pytest.raises(BillingAccessDeniedError):
                view_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
            with pytest.raises(BillingAccessDeniedError):
                list_invoices(clinic_id=setup.clinic_id)
    with runtime_role():
        assert not Invoice.objects.exists()
        assert patient_charges() == ()


@pytest.mark.parametrize("role", ["owner", "clinic_admin"])
def test_canonical_billing_roles_and_revocation(
    rbac_graph: RbacGraph, role: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with fixture_scope(setup.organization_id):
        assignment = UserClinicRole.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user_id=rbac_graph.clinic_admin,
            role=role,
        )
    with runtime_role(), tenant_context(rbac_graph.clinic_admin, setup.organization_id):
        invoice = charge(setup)
        assert [i.pk for i in list_invoices(clinic_id=setup.clinic_id)] == [invoice.pk]
    with fixture_scope(setup.organization_id):
        assignment.delete()
    with runtime_role(), tenant_context(rbac_graph.clinic_admin, setup.organization_id):
        assert not Invoice.objects.exists()
        with pytest.raises(BillingAccessDeniedError):
            view_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)


def test_patient_sees_only_own_released_charge_and_receipt(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invitation = issue_invitation(
            clinic_id=setup.clinic_id, enrollment_id=setup.enrollment_id
        )
        draft = charge(setup)
        hidden = charge(setup, issued=True)
        visible = charge(setup, issued=True)
        release_invoice(clinic_id=setup.clinic_id, invoice_id=visible.pk)
        other = create_patient(
            clinic_id=setup.clinic_id,
            full_name="Other Synthetic Patient",
            birth_date=date(2001, 1, 1),
            idempotency_key=uuid4(),
        )
        other_invoice = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=other.patient.pk,
            amount_minor=88888,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=setup.clinic_id, invoice_id=other_invoice.pk, expected_revision=1
        )
        release_invoice(clinic_id=setup.clinic_id, invoice_id=other_invoice.pk)
    with runtime_role():
        session = redeem_invitation(setup.clinic_id, invitation.secret)
        assert session is not None
        with patient_session_context(session) as binding:
            assert binding is not None
            assert "billing" in binding.operations
            rows = patient_charges()
            assert len(rows) == 1
            assert rows[0].invoice_id == visible.pk
            assert rows[0].receipt_reference is None
            assert rows[0].invoice_id not in {draft.pk, hidden.pk, other_invoice.pk}
            assert set(asdict(rows[0])) == {
                "invoice_id",
                "reference",
                "amount_minor",
                "currency",
                "state",
                "issued_at",
                "receipt_reference",
                "receipt_issued_at",
            }
            for model in (Invoice, InvoiceRevision, Settlement, Receipt):
                assert not model.objects.exists()
            with pytest.raises(BillingAccessDeniedError):
                list_invoices(clinic_id=setup.clinic_id)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        receipt = confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=visible.pk,
            confirmation_reference=uuid4(),
            amount_minor=12345,
        )
    with runtime_role(), patient_session_context(session):
        (row,) = patient_charges()
        assert row.state == "paid"
        assert row.amount_minor == receipt.amount_minor
        assert row.receipt_reference == receipt.reference
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        revoke_patient_access(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            grant_id=invitation.grant.pk,
        )
    with runtime_role(), patient_session_context(session) as binding:
        assert binding is None
        assert patient_charges() == ()


@pytest.mark.parametrize("denial", ["operation", "expiry", "grant_revoked"])
def test_patient_resolver_rechecks_live_session(
    rbac_graph: RbacGraph, denial: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invitation = issue_invitation(
            clinic_id=setup.clinic_id, enrollment_id=setup.enrollment_id
        )
        invoice = charge(setup, issued=True)
        release_invoice(clinic_id=setup.clinic_id, invoice_id=invoice.pk)
    if denial == "operation":
        with fixture_scope(setup.organization_id):
            PatientAccessGrant.objects.filter(pk=invitation.grant.pk).update(
                operations=["records"]
            )
    with runtime_role():
        session = redeem_invitation(setup.clinic_id, invitation.secret)
        assert session is not None
    with fixture_scope(setup.organization_id):
        if denial == "expiry":
            PatientSession.objects.filter(pk=session).update(
                idle_expires_at=timezone.now() - timedelta(seconds=1)
            )
        elif denial == "grant_revoked":
            PatientAccessGrant.objects.filter(pk=invitation.grant.pk).update(
                revoked_at=timezone.now()
            )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_patient_session', %s, true)", [str(session)]
        )
        assert patient_charges() == ()


def test_parallel_confirmations_converge_and_outer_failure_rolls_back(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    confirmation = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        invoice = charge(setup, issued=True)
        with transaction.atomic():
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=invoice.pk,
                confirmation_reference=confirmation,
                amount_minor=12345,
            )
            transaction.set_rollback(True)
        invoice.refresh_from_db()
        assert invoice.state == "open"
        assert not Settlement.objects.exists()
        assert not Receipt.objects.exists()
    barrier = Barrier(2, timeout=15)

    def settle() -> UUID:
        try:
            with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
                barrier.wait()
                return confirm_settlement(
                    clinic_id=setup.clinic_id,
                    invoice_id=invoice.pk,
                    confirmation_reference=confirmation,
                    amount_minor=12345,
                ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(settle) for _ in range(2)]
        ids = [future.result(timeout=30) for future in futures]
    assert ids[0] == ids[1]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Receipt.objects.count() == Settlement.objects.count() == 1


def test_patient_session_cannot_cross_clinic_even_for_same_patient(
    rbac_graph: RbacGraph,
) -> None:
    first, second = seed_cross_clinic_appointment_setups(rbac_graph)
    with runtime_role(), tenant_context(first.actor_id, first.organization_id):
        invitation = issue_invitation(
            clinic_id=first.clinic_id, enrollment_id=first.enrollment_id
        )
        own = charge(first, issued=True)
        foreign = charge(second, issued=True)
        release_invoice(clinic_id=first.clinic_id, invoice_id=own.pk)
        release_invoice(clinic_id=second.clinic_id, invoice_id=foreign.pk)
        confirm_settlement(
            clinic_id=second.clinic_id,
            invoice_id=foreign.pk,
            confirmation_reference=uuid4(),
            amount_minor=12345,
        )
    with runtime_role():
        session = redeem_invitation(first.clinic_id, invitation.secret)
        assert session is not None
        with patient_session_context(session):
            assert [row.invoice_id for row in patient_charges()] == [own.pk]


def test_terminal_states_idempotent_release_and_unknown_record(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        cancelled = charge(setup)
        cancel_invoice(clinic_id=setup.clinic_id, invoice_id=cancelled.pk)
        assert (
            cancel_invoice(clinic_id=setup.clinic_id, invoice_id=cancelled.pk).state
            == "cancelled"
        )
        with pytest.raises(BillingConflictError):
            issue_invoice(
                clinic_id=setup.clinic_id, invoice_id=cancelled.pk, expected_revision=1
            )
        with pytest.raises(BillingAccessDeniedError):
            view_invoice(clinic_id=setup.clinic_id, invoice_id=uuid4())
        issued = charge(setup, issued=True)
        assert (
            issue_invoice(
                clinic_id=setup.clinic_id, invoice_id=issued.pk, expected_revision=1
            ).issued_at
            == issued.issued_at
        )
        first = release_invoice(clinic_id=setup.clinic_id, invoice_id=issued.pk)
        second = release_invoice(clinic_id=setup.clinic_id, invoice_id=issued.pk)
        assert first.released_at == second.released_at
        confirmation = uuid4()
        confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=issued.pk,
            confirmation_reference=confirmation,
            amount_minor=12345,
        )
        duplicate = charge(setup, issued=True)
        with pytest.raises(DatabaseError), transaction.atomic():
            confirm_settlement(
                clinic_id=setup.clinic_id,
                invoice_id=duplicate.pk,
                confirmation_reference=confirmation,
                amount_minor=12345,
            )
        duplicate.refresh_from_db()
        assert duplicate.state == "open"
        assert Settlement.objects.count() == Receipt.objects.count() == 1


def test_exact_billing_database_policies_grants_and_function_ownership() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT relname, relrowsecurity, relforcerowsecurity,
            relowner::regrole::text FROM pg_class
            JOIN pg_namespace n ON n.oid = relnamespace
            WHERE n.nspname = 'clinic_app' AND relname = ANY(%s)""",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (t, True, True, "clinic_owner") for t in TABLES
        }
        cursor.execute(
            """SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies
            WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)""",
            [list(TABLES)],
        )
        policies = cursor.fetchall()
        assert {(r[0], r[1], r[2]) for r in policies} == {
            (t, "billing_staff", "ALL") for t in TABLES
        }
        for table, _, _, using, check in policies:
            assert using == check
            assert "organization_id" in using
            assert "app.current_tenant" in using
            assert (
                "questionnaire_staff"
                if table == "billing_invoice"
                else "billing_staff_invoice"
            ) in using
        cursor.execute(
            """SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
            AND table_name = ANY(%s)""",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {(t, "SELECT") for t in TABLES} | {
            ("billing_invoice", "INSERT"),
            ("billing_settlement", "INSERT"),
        }
        cursor.execute(
            """SELECT table_name, column_name FROM information_schema.role_column_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
            AND privilege_type = 'UPDATE' AND table_name = ANY(%s)""",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("billing_invoice", c)
            for c in (
                "amount_minor",
                "currency",
                "revision",
                "state",
                "issued_at",
                "released_at",
            )
        }
        cursor.execute("""SELECT p.proname, p.proowner::regrole::text,
            p.prosecdef, p.proconfig
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'clinic_app' AND p.proname LIKE 'billing_%%'""")
        functions = cursor.fetchall()
        # Thirteen task-37/38/39 functions plus the task-40 patient payment
        # resolver; every one stays resolver-owned with a fixed search path.
        assert len(functions) == 15
        assert all(
            owner
            == ("clinic_owner" if name == "billing_pix_guard" else "clinic_resolver")
            and config == ["search_path=pg_catalog, clinic_app, pg_temp"]
            and secure == (name != "billing_pix_guard")
            for name, owner, secure, config in functions
        )
        cursor.execute("""SELECT p.proname, a.grantee::regrole::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,
            LATERAL aclexplode(p.proacl) a
            WHERE n.nspname = 'clinic_app' AND p.proname LIKE 'billing_%%'
            AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner""")
        assert set(cursor.fetchall()) == {
            ("billing_patient_charge", "clinic_app"),
            ("billing_patient_charges", "clinic_app"),
            ("billing_staff_invoice", "clinic_app"),
            ("billing_payment_event_scope", "clinic_app"),
            ("billing_payment_event_seen", "clinic_app"),
            ("billing_payment_event_recorder", "clinic_app"),
            ("billing_payment_event_superseded", "clinic_app"),
            ("billing_payment_event_recovered", "clinic_app"),
        }
