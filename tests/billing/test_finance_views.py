"""Native Finance forms, accountant export and translated financial projections."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, time, timedelta
from importlib import import_module
from io import StringIO
from typing import TYPE_CHECKING, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from apps.audit.models import AuditEvent
from apps.billing.cash import open_cash
from apps.billing.compensation import create_payout_rule
from apps.billing.exports import export_journal
from apps.billing.finance_commands import HANDLERS
from apps.billing.finance_forms import ACTION_FIELDS
from apps.billing.finance_views import REFUSED, _command_scope
from apps.billing.journal import Line, SourceFact, post_entry
from apps.billing.models import CashSession, InvoiceTerms, JournalBook
from apps.billing.money_types import PayoutRuleTerms
from apps.identity.models import RoleGrant
from apps.scheduling.agenda_presenter import clinic_local_today
from apps.tenancy.db import tenant_context
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext

from billing.journal_support import (
    FinanceSetup,
    collected,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from core.test_navigation import _client_for, _get, _reauthenticate
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_billing_views import staff_client

if TYPE_CHECKING:
    from collections.abc import Callable
    from xml.etree.ElementTree import Element

    from django.http import HttpRequest

# Keep hardened XML parsing at an explicitly typed untyped-library boundary.
fromstring = cast(
    "Callable[[bytes], Element]", import_module("defusedxml.ElementTree").fromstring
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_finance_lists_percentage_and_fixed_rules_with_their_policy(
    finance_setup: FinanceSetup,
) -> None:
    # Given: both supported rule kinds exist in the clinic's book.
    with (
        runtime_role(),
        tenant_context(
            finance_setup.graph.shared_user, finance_setup.graph.organization_a
        ),
    ):
        for kind in ("percent", "fixed"):
            create_payout_rule(
                clinic_id=finance_setup.clinic_id,
                practitioner_id=finance_setup.graph.physician,
                terms=PayoutRuleTerms(kind, 3000),
                command_id=uuid4(),
            )
    with staff_client(finance_setup.graph.clinic_admin) as client:
        # When: staff open the actual rule-backed workspace.
        response = client.get(
            reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
        )
        # Then: percentage and centavo rules use distinct units and stored policy.
        assert response.status_code == 200
        rule_form = next(
            form for form in response.context["forms"] if form.action == "prepare_terms"
        )
        labels = [label for key, label in rule_form.fields["rule_id"].choices if key]
        assert len(labels) == 2
        assert any("30,00%" in label for label in labels)
        assert any("R$ 30,00" in label for label in labels)
        assert all("completion-net-v1" in label for label in labels)


def test_finance_renders_posted_bigint_aggregate_balances_as_exact_reais(
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup):
        post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(
                Line("bank", "debit", 1001),
                Line("opening_balances", "credit", 1001),
            ),
        )
    with staff_client(finance_setup.graph.clinic_admin) as client:
        response = client.get(
            reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
        )
        assert response.status_code == 200
        assert b"R$ 10,01" in response.content
        assert gettext("Bank").encode() in response.content


def test_every_native_action_has_exactly_one_command_handler() -> None:
    assert set(HANDLERS) == set(ACTION_FIELDS)


def _clinic_and_utc_today(
    monkeypatch: pytest.MonkeyPatch, zone: str, posted_at: datetime
) -> tuple[date, date]:
    """Read today under a clock at 01:30Z, 22:30 of the posting's clinic day.

    Exports select clinic-local days; Django's TIME_ZONE (UTC) is already on
    the next date there, the window in which a UTC-derived day exports nothing.
    """
    posted_day = timezone.localdate(posted_at, ZoneInfo(zone))
    pinned = datetime.combine(posted_day + timedelta(days=1), time(1, 30), tzinfo=UTC)
    with monkeypatch.context() as clock:
        clock.setattr("django.utils.timezone.now", lambda: pinned)
        today = date.fromisoformat(clinic_local_today(zone))
        utc_today = timezone.localdate()
    assert (today, utc_today) == (posted_day, posted_day + timedelta(days=1))
    return today, utc_today


def _sealed_fact(setup: FinanceSetup) -> tuple[datetime, str]:
    """Post one fact with no free text; return its instant and the clinic zone."""
    with finance_scope(setup):
        entry = post_entry(
            book_id=setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(
                Line("bank", "debit", 1001),
                Line("opening_balances", "credit", 1001),
            ),
        )
        zone = JournalBook.objects.get(pk=setup.book_id).clinic.timezone
    # The sealing trigger sets posted_at for every posted entry.
    return cast("datetime", entry.posted_at), str(zone)


def test_journal_export_route_returns_the_accountant_download(
    finance_setup: FinanceSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a sealed fact, and today as the clinic sees it at 01:30Z.
    posted_at, zone = _sealed_fact(finance_setup)
    day, _utc_today = _clinic_and_utc_today(monkeypatch, zone, posted_at)
    with staff_client(finance_setup.graph.clinic_admin) as client:
        # When: the actual POST route receives a valid date window.
        response = client.post(
            reverse(
                "billing:journal-export",
                kwargs={"clinic_id": finance_setup.clinic_id},
            ),
            {"start": day.isoformat(), "end": day.isoformat(), "format": "csv"},
        )
        # Then: it returns an accountant file, not a decorator callable.
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"] == (
            'attachment; filename="clinic-journal.csv"'
        )
        assert response.content.startswith(b"entry_id,posted_at,")
        # And: it carries the clinic day's sealed lines, not an empty window.
        rows = tuple(csv.DictReader(StringIO(response.content.decode())))
        assert sorted((row["side"], row["amount_minor"]) for row in rows) == [
            ("credit", "1001"),
            ("debit", "1001"),
        ]


def test_finance_native_open_and_replay_use_the_stored_journal(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a real Finance session and the existing clinic book.
    url = reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
    with staff_client(finance_setup.graph.clinic_admin) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert gettext("Day close").encode() in response.content
        # When: native POST opens a zero drawer with a stable command.
        command = uuid4()
        response = client.post(
            url, {"action": "open_cash", "counted": "0,00", "command_id": command}
        )
        assert response.status_code == 302
        assert response["Location"] == url
        repeated = client.post(
            url, {"action": "open_cash", "counted": "0,00", "command_id": command}
        )
        assert repeated.status_code == 302
    # Then: retry did not open another drawer.
    with finance_scope(finance_setup):
        assert CashSession.objects.filter(book_id=finance_setup.book_id).count() == 1


def test_finance_unknown_and_wrong_clinic_are_identical_denials(
    finance_setup: FinanceSetup,
) -> None:
    with staff_client(finance_setup.graph.clinic_admin) as client:
        # When/Then: neither selector reveals another clinic's journal.
        responses = [
            client.get(reverse("billing:finance", kwargs={"clinic_id": clinic_id}))
            for clinic_id in (finance_setup.graph.clinic_c, uuid4())
        ]
        assert [response.status_code for response in responses] == [403, 403]


def test_scoped_csv_and_ofx_like_exports_have_identical_exact_line_values(
    finance_setup: FinanceSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one sealed fact with no identifying free-text content, and today
    # as the clinic sees it at 01:30Z, when Django's UTC date is tomorrow.
    posted_at, zone = _sealed_fact(finance_setup)
    day, utc_day = _clinic_and_utc_today(monkeypatch, zone, posted_at)
    with finance_scope(finance_setup):
        # When: the two documented accountant formats project today's clinic day.
        csv_bytes = export_journal(
            clinic_id=finance_setup.clinic_id, start=day, end=day, output_format="csv"
        )
        xml_bytes = export_journal(
            clinic_id=finance_setup.clinic_id,
            start=day,
            end=day,
            output_format="journal",
        )
        # Then: both retain integer centavos and side, without a rounding conversion.
        rows = tuple(csv.DictReader(StringIO(csv_bytes.decode())))
        assert sorted((row["side"], row["amount_minor"]) for row in rows) == [
            ("credit", "1001"),
            ("debit", "1001"),
        ]
        document = fromstring(xml_bytes)
        assert document.attrib["version"] == "clinic-journal-v1"
        assert sorted(
            (line.findtext("side"), line.findtext("amount_minor"))
            for line in document.findall("./entry/line")
        ) == [
            ("credit", "1001"),
            ("debit", "1001"),
        ]
        # And: the previous clinic day and the clock's UTC date (the next
        # clinic day) hold only the header row.
        for neighbour in (day - timedelta(days=1), utc_day):
            assert (
                export_journal(
                    clinic_id=finance_setup.clinic_id,
                    start=neighbour,
                    end=neighbour,
                    output_format="csv",
                )
                .decode()
                .count("\n")
                == 1
            )


def _journal_views(setup: FinanceSetup) -> int:
    with owner_context(setup.graph.organization_a):
        return AuditEvent.objects.filter(
            event_type="billing.journal.viewed", payload__clinic_id=str(setup.clinic_id)
        ).count()


def test_collection_without_charge_read_keeps_its_actions_but_no_charge_panels(
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
    views = _journal_views(finance_setup)
    reader = _get(client, url)
    # Then: the charge, its amount and the journal view are shown and audited.
    assert reader.status_code == 200
    assert reference in reader.content.decode()
    assert b"R$ 100,00" in reader.content
    assert f'href="{charges}"' in reader.content.decode()
    assert _journal_views(finance_setup) == views + 1
    # When: the clinic narrows charge.read and keeps reception's charge.collect.
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role="receptionist",
            permission="charge.read",
            valid_from=timezone.now() - timedelta(minutes=1),
        )
    _reauthenticate(client, user.pk, "receptionist")
    collector = _get(client, url)
    # Then: collection keeps its commands but sees no charge, amount or panel.
    html = collector.content.decode()
    assert collector.status_code == 200
    assert 'data-finance-read="denied"' in html
    for action in ("observe", "open_cash", "close_cash"):
        assert f'name="action" value="{action}"' in html
    assert reference not in html
    assert "R$ 100,00" not in html
    assert 'id="receivables-caption"' not in html
    assert 'id="collections-caption"' not in html
    assert 'id="accounts-caption"' not in html
    assert f'href="{charges}"' not in html
    assert _journal_views(finance_setup) == views + 1


def test_finance_answers_invalid_fields_with_the_bound_form(
    finance_setup: FinanceSetup,
) -> None:
    url = reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
    with staff_client(finance_setup.graph.clinic_admin) as client:
        # When: a permitted command arrives with a negative cash count.
        response = client.post(
            url, {"action": "open_cash", "counted": "-1,00", "command_id": uuid4()}
        )
        # Then: the bound form is refused as invalid and nothing is opened.
        assert response.status_code == 400
        assert gettext("Check the financial fields below.").encode() in response.content
    with finance_scope(finance_setup):
        assert not CashSession.objects.filter(book_id=finance_setup.book_id).exists()


def test_finance_refuses_a_stale_drawer_revision_as_a_conflict(
    finance_setup: FinanceSetup,
) -> None:
    # Given: one open drawer at its first revision.
    with finance_scope(finance_setup):
        drawer = open_cash(
            clinic_id=finance_setup.clinic_id, counted_minor=0, command_id=uuid4()
        )
    url = reverse("billing:finance", kwargs={"clinic_id": finance_setup.clinic_id})
    with staff_client(finance_setup.graph.clinic_admin) as client:
        # When: a close names a revision the reviewer never saw.
        response = client.post(
            url,
            {
                "action": "close_cash",
                "session_id": str(drawer.pk),
                "counted": "0,00",
                "expected_revision": drawer.revision + 1,
                "command_id": uuid4(),
            },
        )
        # Then: it is a conflict and the drawer stays open, unchanged.
        assert response.status_code == 409
        assert gettext(REFUSED).encode() in response.content
    with finance_scope(finance_setup):
        current = CashSession.objects.get(pk=drawer.pk)
        assert (current.state, current.revision) == ("open", drawer.revision)


def test_journal_export_refuses_an_inverted_window(
    finance_setup: FinanceSetup,
) -> None:
    day = date(2033, 5, 18)
    url = reverse(
        "billing:journal-export", kwargs={"clinic_id": finance_setup.clinic_id}
    )
    with staff_client(finance_setup.graph.clinic_admin) as client:
        # When: the export window ends before it starts.
        response = client.post(
            url,
            {
                "start": day.isoformat(),
                "end": (day - timedelta(days=1)).isoformat(),
                "format": "csv",
            },
        )
        # Then: no accountant file is produced.
        assert response.status_code == 400
        assert response.content.decode() == gettext("Check the export dates.")
        assert not response.has_header("Content-Disposition")


def _keyless_step_up(request: HttpRequest) -> HttpResponse:
    """Run the actual step-up scope with SessionMiddleware's unsaved key."""
    with _command_scope(request, uuid4(), "reopen_cash"):
        return HttpResponse(status=204)


urlpatterns = [path("auth/login/", _keyless_step_up)]


def test_step_up_commands_refuse_a_request_without_a_framework_session_key() -> None:
    # Authenticated Finance requests always carry a key, so a thin adapter runs
    # the real scope on a real anonymous session instead of pretending auth.
    with override_settings(ROOT_URLCONF=__name__):
        response = Client(enforce_csrf_checks=True).get("/auth/login/")
    assert response.status_code == 403
    assert response.wsgi_request.session.session_key is None
    assert not response.cookies
