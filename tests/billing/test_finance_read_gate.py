"""Finance offers only what PostgreSQL's book lock admits: charge.read first."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from io import StringIO
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.billing.cash import open_cash
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.exports import export_journal
from apps.billing.finance_commands import execute_command
from apps.billing.finance_forms import ACTION_FIELDS, FinanceForm
from apps.billing.finance_queries import ACTION_PERMISSIONS, finance_context
from apps.billing.finance_views import DENIED, REFUSED
from apps.billing.models import (
    CashSession,
    CashSessionEvent,
    InvoiceTerms,
    JournalBook,
    JournalEntry,
    JournalLine,
    JournalSettlement,
    Payout,
    PractitionerPayoutRule,
    Refund,
    SettlementAllocation,
)
from apps.billing.payouts import request_payout
from apps.billing.refunds import request_refund
from apps.identity.models import RoleGrant, UserClinicRole
from apps.scheduling.agenda_presenter import clinic_local_today
from apps.tenancy.db import tenant_context
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from auth.stepup_test_support import create_role_actor
from billing.journal_support import (
    FinanceSetup,
    allocated,
    collected,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from core.test_navigation import _client_for, _get, _post, _reauthenticate
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from django.db.models import Model

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)

# Every command offered once the book is open; each takes billing_lock_book.
BOOK_COMMANDS: Final = tuple(sorted(set(ACTION_FIELDS) - {"create_book"}))
# Every row a refused command could have written.
LEDGER: Final[Mapping[type[Model], tuple[str, ...]]] = {
    CashSession: ("id", "state", "revision"),
    CashSessionEvent: ("id",),
    InvoiceTerms: ("id", "completion_entry_id"),
    JournalEntry: ("id", "state"),
    JournalLine: ("id",),
    JournalSettlement: ("id", "state"),
    Payout: ("id", "state"),
    PractitionerPayoutRule: ("id",),
    Refund: ("id", "state"),
    SettlementAllocation: ("id",),
}
SCALARS: Final = {
    "account": "bank",
    "adjustment": "0,00",
    "amount": "1,00",
    "attested": "on",
    "counted": "0,00",
    "discount": "0,00",
    "expected_revision": "1",
    "kind": "percent",
    "outcome": "sending",
    "payer": "patient",
    "reason": "correction",
    "receipt_kind": "patient",
    "refund_kind": "refund",
    "rule_value": "3000",
}


def _narrow(graph: RbacGraph, role: str) -> None:
    """The clinic subtracts charge.read from a role and keeps its other grants."""
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role=role,
            permission="charge.read",
            valid_from=timezone.now() - timedelta(minutes=1),
        )


def _ledger(setup: FinanceSetup) -> tuple[frozenset[object], ...]:
    """Money rows and billing audit events, read by a charge reader and owner."""
    with finance_scope(setup):
        rows = tuple(
            frozenset(model._default_manager.values_list(*fields))
            for model, fields in LEDGER.items()
        )
    with owner_context(setup.graph.organization_a):
        events = frozenset(
            AuditEvent.objects.filter(
                event_type__startswith="billing.",
                payload__clinic_id=str(setup.clinic_id),
            ).values_list("pk", flat=True)
        )
    return (*rows, events)


def _events(setup: FinanceSetup, event_type: str) -> int:
    with owner_context(setup.graph.organization_a):
        return AuditEvent.objects.filter(
            event_type=event_type, payload__clinic_id=str(setup.clinic_id)
        ).count()


def _holder(action: str) -> str:
    """Finance holds every command permission but the policy owner's."""
    return "org_admin" if "finance.policy" in ACTION_PERMISSIONS[action] else "finance"


def _book_world(setup: FinanceSetup) -> Mapping[str, Sequence[tuple[str, str]]]:
    """Seed a row behind every selector; return a charge reader's choices."""
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
        _context, choices = finance_context(clinic_id=setup.clinic_id)
    return choices


def _command(
    action: str, choices: Mapping[str, Sequence[tuple[str, str]]]
) -> dict[str, str]:
    """One complete submission, as a charge reader's workspace accepts it."""
    data = {"action": action}
    for name in ACTION_FIELDS[action]:
        if name in choices:
            data[name] = next(key for key, _label in choices[name] if key)
        elif name in {"command_id", "evidence_reference"}:
            data[name] = str(uuid4())
        elif name != "outcome_evidence":
            data[name] = SCALARS[name]
    return data


def _form(action: str, choices: Mapping[str, Sequence[tuple[str, str]]]) -> FinanceForm:
    data = QueryDict(mutable=True)
    data.update(_command(action, choices))
    form = FinanceForm(data, action=action, choices=choices)
    assert form.is_valid(), (action, form.errors)
    return form


def test_every_book_command_refuses_a_holder_without_charge_read(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a row behind every selector, and holders of each command's permission.
    choices = _book_world(finance_setup)
    graph = finance_setup.graph
    holders = {role: _client_for(graph, role) for role in ("finance", "org_admin")}
    url = reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
    # A charge reader with those permissions is offered every book command.
    offered = {
        form.action
        for client, _user in holders.values()
        for form in _get(client, url).context["forms"]
    }
    assert offered == set(BOOK_COMMANDS)
    # When: the clinic narrows charge.read from both roles.
    for role, (client, user) in holders.items():
        _narrow(graph, role)
        _reauthenticate(client, user.pk, role)
    for action in BOOK_COMMANDS:
        client, _user = holders[_holder(action)]
        before = _ledger(finance_setup)
        # Then: each complete submission is a permission denial, not a conflict.
        response = _post(client, url, _command(action, choices))
        html = response.content.decode()
        assert response.status_code == 403, action
        assert gettext(DENIED) in html
        assert gettext(REFUSED) not in html
        assert 'data-finance-read="denied"' in html
        assert 'name="action"' not in html
        # And: it wrote no money row, journal line or billing audit event.
        assert _ledger(finance_setup) == before, action


def test_book_lock_refusal_is_a_permission_denial_in_every_command_service(
    finance_setup: FinanceSetup,
) -> None:
    # Given: valid commands, exactly as a charge reader's workspace submits them.
    choices = _book_world(finance_setup)
    forms = {action: _form(action, choices) for action in BOOK_COMMANDS}
    graph = finance_setup.graph
    holders = {
        role: create_role_actor(graph, UserClinicRole.Role(role))
        for role in ("finance", "org_admin")
    }
    for role in holders:
        _narrow(graph, role)
    for action, form in forms.items():
        before = _ledger(finance_setup)
        # When: a holder without charge.read runs the actual command service,
        # bypassing the view: the refund and payout requests' own charge.read
        # check or else PostgreSQL's book lock refuses it there.
        with (
            runtime_role(),
            tenant_context(holders[_holder(action)].pk, graph.organization_a),
            pytest.raises(BillingAccessDeniedError),
        ):
            execute_command(clinic_id=finance_setup.clinic_id, form=form)
        # Then: the refusal is the permission denial, with nothing written.
        assert _ledger(finance_setup) == before, action


def _open() -> dict[str, str]:
    return {"action": "open_cash", "counted": "0,00", "command_id": str(uuid4())}


def _close(drawer: CashSession) -> dict[str, str]:
    return {
        "action": "close_cash",
        "session_id": str(drawer.pk),
        "counted": "0,00",
        "expected_revision": str(drawer.revision),
        "command_id": str(uuid4()),
    }


def _observe() -> dict[str, str]:
    return {
        "action": "observe",
        "amount": "25,00",
        "receipt_kind": "patient",
        "account": "bank",
        "evidence_reference": str(uuid4()),
    }


def _open_drawer(setup: FinanceSetup) -> CashSession:
    with finance_scope(setup):
        return CashSession.objects.get(book_id=setup.book_id, state="open")


def test_collection_without_charge_read_is_refused_and_shown_no_charges(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a frozen charge and a posted collection in the clinic's book.
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        collected(finance_setup, 10000)
        reference = str(
            InvoiceTerms.objects.select_related("invoice")
            .get(pk=terms.pk)
            .invoice.reference
        )[:8]
    graph = finance_setup.graph
    client, user = _client_for(graph, "receptionist")
    url = reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
    charges = reverse("billing:charges", kwargs={"clinic_id": finance_setup.clinic_id})
    # When: reception, a charge reader, opens Finance.
    views = _events(finance_setup, "billing.journal.viewed")
    reader = _get(client, url)
    # Then: the charge, its amount and the journal view are shown and audited.
    assert reader.status_code == 200
    assert reference in reader.content.decode()
    assert b"R$ 100,00" in reader.content
    assert f'href="{charges}"' in reader.content.decode()
    assert _events(finance_setup, "billing.journal.viewed") == views + 1
    # And: its collection commands open and close a drawer and observe money.
    assert _post(client, url, _open()).status_code == 302
    assert _post(client, url, _close(_open_drawer(finance_setup))).status_code == 302
    assert _post(client, url, _open()).status_code == 302
    assert _post(client, url, _observe()).status_code == 302
    assert _get(client, url).status_code == 200
    drawer = _open_drawer(finance_setup)
    # When: the clinic narrows charge.read and keeps reception's charge.collect.
    _narrow(graph, "receptionist")
    _reauthenticate(client, user.pk, "receptionist")
    before = _ledger(finance_setup)
    collector = _get(client, url)
    # Then: Finance states the denial and shows no charge, amount or command.
    html = collector.content.decode()
    assert collector.status_code == 200
    assert 'data-finance-read="denied"' in html
    assert collector.context["forms"] == []
    assert 'name="action"' not in html
    assert reference not in html
    assert "R$" not in html
    for caption in ("cash", "receivables", "collections", "accounts"):
        assert f'id="{caption}-caption"' not in html
    assert f'href="{charges}"' not in html
    # And: each submitted collection command is a permission denial.
    for data in (_observe(), _open(), _close(drawer)):
        refused = _post(client, url, data)
        assert refused.status_code == 403, data["action"]
        assert gettext(DENIED) in refused.content.decode()
        assert gettext(REFUSED) not in refused.content.decode()
        # With no drawer, collection, journal line or audit event, viewed included.
        assert _ledger(finance_setup) == before, data["action"]


def test_journal_export_and_choices_need_charge_read(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a frozen charge whose posted collection sealed journal lines.
    invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        collected(finance_setup, 10000)
        zone = str(JournalBook.objects.get(pk=finance_setup.book_id).clinic.timezone)
    today = date.fromisoformat(clinic_local_today(zone))
    window = {
        "start": (today - timedelta(days=1)).isoformat(),
        "end": (today + timedelta(days=1)).isoformat(),
        "format": "csv",
    }
    graph = finance_setup.graph
    client, user = _client_for(graph, "finance")
    url = reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
    export = reverse(
        "billing:journal-export", kwargs={"clinic_id": finance_setup.clinic_id}
    )
    viewed = _events(finance_setup, "billing.journal.viewed")
    # When: Finance, a charge reader, opens the workspace and downloads the journal.
    reader = _get(client, url)
    download = _post(client, export, window)
    # Then: the export form and amount-bearing choices are shown, the view audited.
    assert 'id="accountant-export-title"' in reader.content.decode()
    verify = next(form for form in reader.context["forms"] if form.action == "verify")
    assert any(
        "R$ 100,00" in label for _key, label in verify["settlement_id"].field.choices
    )
    assert download.status_code == 200
    rows = tuple(csv.DictReader(StringIO(download.content.decode())))
    assert "10000" in {row["amount_minor"] for row in rows}
    # And: a refused command re-renders the journal and is audited as viewed too.
    invalid = _post(
        client, url, {"action": "open_cash", "counted": "-1,00", "command_id": "x"}
    )
    assert invalid.status_code == 400
    assert "R$ 100,00" in invalid.content.decode()
    assert _events(finance_setup, "billing.journal.viewed") == viewed + 2
    # When: the clinic narrows Finance's charge.read and keeps settlement.post.
    _narrow(graph, "finance")
    _reauthenticate(client, user.pk, "finance")
    before = _ledger(finance_setup)
    narrowed = _get(client, url)
    # Then: no export form, command, choice or amount is shown.
    html = narrowed.content.decode()
    assert 'data-finance-read="denied"' in html
    assert 'id="accountant-export-title"' not in html
    assert narrowed.context["forms"] == []
    assert "R$" not in html
    # And: the route refuses before it reads the window, and writes no file.
    for data in (window, {**window, "start": "not-a-date"}):
        refused = _post(client, export, data)
        assert refused.status_code == 403
        assert not refused.has_header("Content-Disposition")
        assert b"amount_minor" not in refused.content
    # And: the services refuse it directly: no choices and no export.
    with runtime_role(), tenant_context(user.pk, graph.organization_a):
        assert finance_context(clinic_id=finance_setup.clinic_id)[1] == {}
        with pytest.raises(BillingAccessDeniedError):
            export_journal(
                clinic_id=finance_setup.clinic_id,
                start=today,
                end=today,
                output_format="csv",
            )
    # With nothing viewed, exported or written while narrowed.
    assert _ledger(finance_setup) == before
