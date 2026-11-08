"""Independent role and scope oracles for the operational journal census."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.finance_queries import permits
from apps.billing.security import financial_actor
from apps.billing.services import (
    BillingAccessDeniedError,
    confirm_settlement,
    create_invoice,
    issue_invoice,
)
from apps.identity.models import RoleGrant, User, UserClinicRole
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from auth.stepup_test_support import create_role_actor
from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)

# Independent expectations from the finance contract, not the production bundle.
PERMISSION_ROLES = {
    "charge.read": {
        "physician",
        "receptionist",
        "scheduler",
        "clinic_admin",
        "clinic_manager",
        "finance",
        "owner",
        "org_admin",
    },
    "charge.create": {"receptionist", "scheduler", "finance"},
    "charge.collect": {"receptionist", "scheduler", "finance"},
    "settlement.post": {"finance"},
    "refund.request": {"clinic_admin", "clinic_manager"},
    "refund.approve": {"finance"},
    "payout.request": {"clinic_admin", "clinic_manager"},
    "payout.approve": {"finance"},
    "finance.policy": {"owner", "org_admin"},
}
STATES = ("current", "narrowed", "revoked", "inactive")
NARROWED_PERMISSION = {
    "owner": "finance.policy",
    "org_admin": "finance.policy",
    "physician": "charge.read",
    "receptionist": "charge.collect",
    "scheduler": "charge.collect",
    "clinic_admin": "refund.request",
    "clinic_manager": "refund.request",
    "finance": "settlement.post",
    "nurse": "appointment.read",
    "allied_professional": "appointment.read",
}


def _subject(setup: FinanceSetup, role: str, state: str) -> UUID:
    graph = setup.graph
    actor = create_role_actor(graph, UserClinicRole.Role(role))
    with owner_context(graph.organization_a):
        if state == "narrowed":
            RoleGrant.objects.create(
                organization_id=graph.organization_a,
                clinic_id=setup.clinic_id,
                role=role,
                permission=NARROWED_PERMISSION[role],
                valid_from=timezone.now(),
            )
        elif state == "revoked":
            UserClinicRole.objects.filter(user=actor).update(revoked_at=timezone.now())
        elif state == "inactive":
            User.objects.filter(pk=actor.pk).update(is_active=False)
    return actor.pk


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
@pytest.mark.parametrize("state", STATES)
def test_financial_permission_roots_use_current_exact_clinic_authority(
    finance_setup: FinanceSetup, role: str, state: str
) -> None:
    actor = _subject(finance_setup, role, state)
    with permission_context(finance_setup.graph, actor):
        for clinic in (
            finance_setup.clinic_id,
            finance_setup.graph.clinic_b,
            finance_setup.graph.clinic_c,
            uuid4(),
        ):
            for permission, roles in PERMISSION_ROLES.items():
                expected = (
                    clinic == finance_setup.clinic_id
                    and role in roles
                    and state not in {"inactive", "revoked"}
                    and not (
                        state == "narrowed" and permission == NARROWED_PERMISSION[role]
                    )
                )
                assert permits(clinic_id=clinic, permission=permission) is expected
                if expected:
                    assert (
                        financial_actor(clinic_id=clinic, permission=permission)
                        == actor
                    )
                else:
                    with pytest.raises(BillingAccessDeniedError):
                        financial_actor(clinic_id=clinic, permission=permission)


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_journal_sql_readers_and_legacy_charge_roots_use_exact_roles(
    finance_setup: FinanceSetup, role: str
) -> None:
    actor = _subject(finance_setup, role, "current")
    finance_roles = {
        "receptionist",
        "scheduler",
        "clinic_admin",
        "clinic_manager",
        "finance",
        "owner",
        "org_admin",
    }
    charge_roles = {"owner", "clinic_admin", "receptionist", "scheduler", "finance"}
    with finance_scope(finance_setup):
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
    with permission_context(finance_setup.graph, actor), connection.cursor() as cursor:
        # Collection authority is resolver-private. Its real entry is the
        # settlement writer and trigger, not a direct runtime function call.
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute(
                "SELECT clinic_app.billing_collection_authorized(%s)",
                [finance_setup.clinic_id],
            )
        for clinic in (
            finance_setup.clinic_id,
            finance_setup.graph.clinic_b,
            finance_setup.graph.clinic_c,
            uuid4(),
        ):
            valid = clinic == finance_setup.clinic_id
            cursor.execute(
                "SELECT clinic_app.billing_finance_read(%s),"
                "clinic_app.billing_charge_authorized(%s)",
                [clinic, clinic],
            )
            assert cursor.fetchone() == (
                valid and role in finance_roles,
                valid and role in charge_roles,
            )
            cursor.execute(
                "SELECT user_id FROM clinic_app.billing_practitioners(%s)", [clinic]
            )
            assert {row[0] for row in cursor.fetchall()} == (
                {finance_setup.graph.physician}
                if valid and role in finance_roles
                else set()
            )
            if valid and role in charge_roles:
                receipt = confirm_settlement(
                    clinic_id=clinic,
                    invoice_id=invoice.pk,
                    confirmation_reference=uuid4(),
                    amount_minor=1000,
                )
                assert receipt.invoice_id == invoice.pk
            else:
                with pytest.raises(BillingAccessDeniedError):
                    confirm_settlement(
                        clinic_id=clinic,
                        invoice_id=invoice.pk,
                        confirmation_reference=uuid4(),
                        amount_minor=1000,
                    )
        for book in (finance_setup.book_id, uuid4()):
            cursor.execute(
                "SELECT clinic_app.billing_book_access(%s,'journal.read'),"
                "clinic_app.billing_book_access(%s,'settlement.post')",
                [book, book],
            )
            assert cursor.fetchone() == (
                book == finance_setup.book_id and role in finance_roles,
                book == finance_setup.book_id and role == "finance",
            )
