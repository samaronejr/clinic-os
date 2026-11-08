"""PostgreSQL itself applies the RP charges/settlements view cell to journal rows.

finance_context reads nothing without charge.read. The row cases bypass Python:
they count rows as clinic_app inside each actor's own tenant context, so only
the forced RLS policies and the SQL choice resolver decide what is returned.
The book row is the one exception, held for every Finance command holder.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.billing.cash import open_cash
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.finance_queries import finance_context
from apps.billing.finance_views import DENIED, REFUSED
from apps.billing.journal import create_book
from apps.billing.payouts import request_payout
from apps.billing.refunds import request_refund
from apps.identity.models import RoleGrant, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from psycopg import connect, sql

from auth.stepup_test_support import create_role_actor
from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from core.test_navigation import _client_for, _get, _post
from identity.permission_support import owner_context
from infra.journal_catalog import TABLES
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)

BOOK: Final = "billing_journalbook"
RECORDS: Final = TABLES - {BOOK}
# Every role whose bundle holds a Finance command (collect, post, request or
# approve refunds and payouts, or the finance policy).
COMMAND_ROLES: Final = (
    "receptionist",
    "scheduler",
    "clinic_manager",
    "clinic_admin",
    "finance",
    "org_admin",
    "owner",
)


def _seed(setup: FinanceSetup) -> None:
    """Put a row in every journal table, as a charge reader's commands do."""
    terms = invoice_terms(setup)
    with finance_scope(setup):
        allocation = allocated(setup, terms, 1000)
        request_refund(
            clinic_id=setup.clinic_id,
            allocation_id=allocation.pk,
            amount_minor=100,
            command_id=uuid4(),
        )
        request_payout(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.graph.physician,
            amount_minor=100,
            command_id=uuid4(),
        )
        open_cash(clinic_id=setup.clinic_id, counted_minor=0, command_id=uuid4())


def _narrow(graph: RbacGraph, role: str) -> None:
    """The clinic subtracts charge.read from one role and keeps its other grants."""
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role=role,
            permission="charge.read",
            valid_from=timezone.now() - timedelta(minutes=1),
        )


def _stored(setup: FinanceSetup, superuser_database_url: str) -> dict[str, int]:
    """Rows per journal table, counted by a superuser that bypasses RLS."""
    with connect(superuser_database_url) as privileged:
        return {
            table: privileged.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE organization_id=%s").format(
                    sql.Identifier("clinic_app", table)
                ),
                [setup.graph.organization_a],
            ).fetchall()[0][0]
            for table in sorted(TABLES)
        }


def _visible(setup: FinanceSetup, actor: UUID) -> dict[str, object]:
    """What PostgreSQL returns to clinic_app in the actor's own tenant context."""
    clinic = setup.clinic_id
    with (
        runtime_role(),
        tenant_context(actor, setup.graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT current_user,clinic_app.has_permission('charge.read',%s,NULL),"
            "clinic_app.billing_finance_read(%s)",
            [clinic, clinic],
        )
        role, charge_reader, command_holder = cursor.fetchall()[0]
        assert role == "clinic_app"
        rows: dict[str, object] = {
            "charge.read": charge_reader,
            "finance_command": command_holder,
        }
        for table in sorted(TABLES):
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier("clinic_app", table)
                )
            )
            rows[table] = cursor.fetchall()[0][0]
        cursor.execute(
            "SELECT count(*) FROM clinic_app.billing_practitioners(%s)", [clinic]
        )
        rows["practitioner_choices"] = cursor.fetchall()[0][0]
    return rows


@pytest.mark.parametrize("role", COMMAND_ROLES)
def test_narrowed_charge_read_returns_no_journal_record_row(
    finance_setup: FinanceSetup, superuser_database_url: str, role: str
) -> None:
    # Given: a row in every journal table and a holder of the role's command.
    _seed(finance_setup)
    stored = _stored(finance_setup, superuser_database_url)
    assert all(stored.values()), stored
    assert stored[BOOK] == 1
    graph = finance_setup.graph
    control_role = "receptionist" if role == "finance" else "finance"
    holder, control, physician = (
        create_role_actor(graph, UserClinicRole.Role(name)).pk
        for name in (role, control_role, "physician")
    )
    # A charge reader in another role sees every stored row and choice.
    reader = _visible(finance_setup, control)
    assert reader["practitioner_choices"]
    assert reader == {
        "charge.read": True,
        "finance_command": True,
        **stored,
        "practitioner_choices": reader["practitioner_choices"],
    }
    assert _visible(finance_setup, holder) == reader
    # When: the clinic narrows charge.read from that role only.
    _narrow(graph, role)
    # Then: the holder keeps its command but PostgreSQL returns no journal
    # record row and no record-bearing choice. The book row stays visible to
    # every Finance command holder, so the workspace states the denial.
    narrowed = _visible(finance_setup, holder)
    assert narrowed == {
        "charge.read": False,
        "finance_command": True,
        **dict.fromkeys(RECORDS, 0),
        BOOK: 1,
        "practitioner_choices": 0,
    }
    # And: the charge reader in another role still reads every row.
    assert _visible(finance_setup, control) == reader
    # And: charge.read without a Finance command is not widened into the book.
    assert _visible(finance_setup, physician) == {
        "charge.read": True,
        "finance_command": False,
        **dict.fromkeys(TABLES, 0),
        "practitioner_choices": 0,
    }


def _books(superuser_database_url: str, graph: RbacGraph) -> int:
    with connect(superuser_database_url) as privileged:
        return int(
            privileged.execute(
                "SELECT count(*) FROM clinic_app.billing_journalbook "
                "WHERE clinic_id=%s",
                [graph.clinic_a],
            ).fetchall()[0][0]
        )


def _opened(graph: RbacGraph) -> int:
    with owner_context(graph.organization_a):
        return AuditEvent.objects.filter(
            event_type="billing.book.created", payload__clinic_id=str(graph.clinic_a)
        ).count()


@pytest.mark.parametrize("role", ["org_admin", "owner"])
def test_opening_the_book_needs_charge_read_like_every_finance_command(
    rbac_graph: RbacGraph, superuser_database_url: str, role: str
) -> None:
    # Given: a clinic without a book that narrowed charge.read from one
    # finance.policy role; the other policy role still reads charges.
    graph = rbac_graph
    url = reverse("billing:finance", kwargs={"clinic_id": graph.clinic_a})
    _narrow(graph, role)
    narrowed, narrowed_user = _client_for(graph, role)
    # When: the narrowed policy owner opens Finance and submits the command.
    page = _get(narrowed, url)
    refused = _post(narrowed, url, {"action": "create_book"})
    # Then: the command is not offered, because seeding the chart takes the
    # book lock, and the submission is a permission denial, not a conflict.
    assert page.status_code == 200
    assert 'name="action"' not in page.content.decode()
    assert refused.status_code == 403
    assert gettext(DENIED) in refused.content.decode()
    assert gettext(REFUSED) not in refused.content.decode()
    with (
        runtime_role(),
        tenant_context(narrowed_user.pk, graph.organization_a),
        pytest.raises(BillingAccessDeniedError),
    ):
        create_book(clinic_id=graph.clinic_a)
    assert (_books(superuser_database_url, graph), _opened(graph)) == (0, 0)
    # When: a charge-reading policy owner opens the book.
    control, _user = _client_for(graph, "owner" if role == "org_admin" else "org_admin")
    assert 'value="create_book"' in _get(control, url).content.decode()
    assert _post(control, url, {"action": "create_book"}).status_code == 302
    assert (_books(superuser_database_url, graph), _opened(graph)) == (1, 1)
    # Then: the narrowed role resolves that book row, so its workspace states
    # the permission denial instead of asking for a book, and reads no record.
    html = _get(narrowed, url).content.decode()
    assert 'data-finance-read="denied"' in html
    assert 'name="action"' not in html
    with runtime_role(), tenant_context(narrowed_user.pk, graph.organization_a):
        context, choices = finance_context(clinic_id=graph.clinic_a)
    assert context["book"] is not None
    assert (context["can_read"], choices) == (False, {})
