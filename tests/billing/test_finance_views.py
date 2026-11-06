"""Native Finance forms, accountant export and translated financial projections."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from importlib import import_module
from io import StringIO
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.billing.compensation import create_payout_rule
from apps.billing.exports import export_journal
from apps.billing.finance_commands import HANDLERS
from apps.billing.finance_forms import ACTION_FIELDS
from apps.billing.journal import Line, SourceFact, post_entry
from apps.billing.models import CashSession
from apps.billing.money_types import PayoutRuleTerms
from apps.tenancy.db import tenant_context
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from patient_service_support import runtime_role
from renewal.test_billing_views import staff_client

if TYPE_CHECKING:
    from collections.abc import Callable
    from xml.etree.ElementTree import Element

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


def test_journal_export_route_returns_the_accountant_download(
    finance_setup: FinanceSetup,
) -> None:
    # Given: a verified Finance user can export today's clinic journal.
    day = timezone.localdate()
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


def test_finance_native_open_and_close_use_the_stored_journal(
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
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup):
        # Given: one sealed fact with no identifying free-text content.
        entry = post_entry(
            book_id=finance_setup.book_id,
            source_fact=SourceFact("manual", uuid4()),
            lines=(
                Line("bank", "debit", 1001),
                Line("opening_balances", "credit", 1001),
            ),
        )
        day = timezone.localdate(entry.posted_at)
        # When: the two documented accountant formats project the same date window.
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
        assert (
            export_journal(
                clinic_id=finance_setup.clinic_id,
                start=day + timedelta(days=1),
                end=day + timedelta(days=1),
                output_format="csv",
            )
            .decode()
            .count("\n")
            == 1
        )
        assert date.fromisoformat(str(day)) == day
