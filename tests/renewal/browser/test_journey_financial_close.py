"""J6: financial close (todo 75 ``journey-financial-close``).

Invoices, settlements, refunds, fees, payouts, fiscal documents and insurer
returns reconcile with explicit exception handling and an auditable
close/reopen procedure. The day starts with the owning todos' money proofs in
this runtime (charge instructions refreshed and receipted exactly, a resubmit
never opening a second charge, recorded PIX/card intents paying only on a
sealed receipt, governed revenue reports); then the reference clinic's finance
persona closes the day in the real Finance workspace: opens the cash drawer,
has a mismatched count refused, closes on the sealed journal balance, reopens
with a reason under step-up and closes again, and the accountant export of the
same journal balances to the centavo. A nurse gets no financial action.
NFS-e issuance (EG-9), TISS submission (EG-8), real PSP money, refunds and
payouts (EG-1/EG-5) stay BLOCKED-ON-EG; the workspace says so.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
from django.utils.translation import gettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

import renewal.browser.test_billing as billing
import renewal.browser.test_billing_psp as psp
import renewal.browser.test_reports as reports
from renewal.browser._fixture_secrets import new_totp_key
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._totp_devices import reset_totp_device
from renewal.browser.flagship_support import capture, sign_in, staff_context
from renewal.browser.test_acceptance_clinical import rerun

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

OK: Final = 200
BLOCKED_COPY: Final = (
    "Refund and payout sending is a local rehearsal. No real money is moved. "
    "PSP and live legs remain BLOCKED-ON-EG."
)
STEP_UP_COPY: Final = "Verify your authenticator again before approving or reopening."


def test_j6_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("journal", "real", "double-entry operational journal (todo 56)")
    flagship.adapter("psp", "synthetic", "recorded-psp-v1 fixture provider")
    flagship.adapter("pix", "synthetic", "synthetic-pix-v1 non-payable code")
    for capability in ("pix", "psp_card", "nfse", "tiss"):
        flagship.blocked(reference_clinic, capability, "unit_a")


def test_j6_charge_instructions_refresh_and_receipt_stay_exact(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J6.invoices-settlements",
        "happy",
        billing.test_charge_instructions_refresh_and_receipt_stay_exact,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_j6_resubmit_never_opens_a_second_charge(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J6.invoices-settlements",
        "failure",
        billing.test_browser_back_and_resubmit_never_opens_a_second_charge,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j6_recorded_pix_pays_only_on_a_sealed_receipt(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J6.psp-settlement",
        "adversarial",
        psp.test_recorded_psp_instructions_never_pay_until_a_sealed_receipt,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        case=("pix", 1280, True),
    )


def test_j6_governed_revenue_reports(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J6.reports",
        "happy",
        reports.test_reports_sources_review_boundaries_and_accessibility,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def _finance(page: Page, base: str, clinic: str) -> None:
    goto_settled(page, f"{base}/billing/clinics/{clinic}/finance/")
    expect(page.locator("[data-finance-workspace]")).to_be_visible()


def _action(page: Page, action: str) -> None:
    """Open one action's disclosure and return with its form visible."""
    form = page.locator(f'form:has(input[name="action"][value="{action}"])')
    details = page.locator("details.panel").filter(has=form)
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    expect(form).to_be_visible()


def _submit(page: Page, action: str) -> None:
    form = page.locator(f'form:has(input[name="action"][value="{action}"])')
    click_to_navigate(form.locator("button[type=submit]"))


def _drawer(page: Page) -> tuple[str, int]:
    row = page.locator("tr[data-cash-state]").first
    state = row.get_attribute("data-cash-state")
    assert state is not None
    return state, int(row.locator("td").nth(1).inner_text().strip())


def _close(page: Page, counted: str, revision: int) -> None:
    _action(page, "close_cash")
    page.locator("#id_close_cash_counted").fill(counted)
    page.locator("#id_close_cash_expected_revision").fill(str(revision))
    _submit(page, "close_cash")


def _step_up(page: Page, reference: ReferenceClinic, key: str) -> None:
    """Pass the real step-up screen the refusal links to, with a fresh seed."""
    totp_key = new_totp_key()
    with psycopg.connect(reference.dsn) as connection:
        reset_totp_device(connection, reference.personas[key].user_id, totp_key)
    click_to_navigate(page.locator('#finance-content a[href^="/auth/step-up/"]'))
    token = TOTP(bytes.fromhex(totp_key), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    click_to_navigate(page.locator("button[type=submit]"))


def _reopen(page: Page, revision: int) -> None:
    _action(page, "reopen_cash")
    page.locator("#id_reopen_cash_reason").select_option("correction")
    page.locator("#id_reopen_cash_expected_revision").fill(str(revision))
    _submit(page, "reopen_cash")


def _export_balanced(page: Page, base: str, clinic: str) -> int:
    tokens = [c for c in page.context.cookies() if "csrf" in c["name"].lower()]
    assert len(tokens) == 1
    today = date.today()  # noqa: DTZ011 - a generous window around the server day
    response = page.request.post(
        f"{base}/billing/clinics/{clinic}/journal-export/",
        form={
            "csrfmiddlewaretoken": str(tokens[0]["value"]),
            "start": (today - timedelta(days=3)).isoformat(),
            "end": (today + timedelta(days=3)).isoformat(),
            "format": "csv",
        },
        headers={"Referer": page.url},
    )
    assert response.status == OK, response.status
    rows = list(csv.DictReader(io.StringIO(response.text())))
    assert rows, "the export holds the day's sealed journal lines"
    # Each sealed entry balances on its own, so the whole export does too.
    balance: dict[str, int] = {}
    for row in rows:
        assert row["side"] in {"debit", "credit"}, row["side"]
        assert row["currency"] == "BRL"
        signed = int(row["amount_minor"]) * (1 if row["side"] == "debit" else -1)
        balance[row["entry_id"]] = balance.get(row["entry_id"], 0) + signed
    unbalanced = sum(1 for total in balance.values() if total)
    assert not unbalanced, json.dumps({"unbalanced_entries": unbalanced})
    return len(rows)


def test_j6_day_close_reopen_and_balanced_export(  # noqa: PLR0915 - one close procedure
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    browser = renewal_page.context.browser
    assert browser is not None
    clinic = reference.unit_a
    owner = staff_context(browser, 1280)
    try:
        page = owner.new_page()
        sign_in(page, renewal_base_url, reference, "org_admin")
        _finance(page, renewal_base_url, clinic)
        # Book and opening balance are organization finance policy (org_admin).
        if page.locator('input[name="action"][value="create_book"]').count():
            _action(page, "create_book")
            _submit(page, "create_book")
        # The day's sealed fact: an attested opening bank balance.
        _action(page, "opening_balance")
        page.locator("#id_opening_balance_account").select_option("bank")
        page.locator("#id_opening_balance_amount").fill("120,00")
        page.locator("#id_opening_balance_evidence_reference").fill(str(uuid4()))
        page.locator("#id_opening_balance_attested").check()
        _submit(page, "opening_balance")
        expect(page.locator("#accounts-caption")).to_be_visible()
    finally:
        owner.close()
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in(page, renewal_base_url, reference, "finance_a")
        _finance(page, renewal_base_url, clinic)
        expect(page.get_by_text(gettext(BLOCKED_COPY))).to_be_visible()
        _action(page, "open_cash")
        page.locator("#id_open_cash_counted").fill("0,00")
        _submit(page, "open_cash")
        state, revision = _drawer(page)
        assert state == "open"
        # Failure branch: a count that disagrees with the journal never closes.
        _close(page, "10,00", revision)
        expect(page.locator('[role="alert"]').first).to_be_visible()
        assert _drawer(page)[0] == "open"
        capture(page, renewal_artifact_root, "journey-financial-close", "mismatch")
        _finance(page, renewal_base_url, clinic)
        _close(page, "0,00", _drawer(page)[1])
        state, revision = _drawer(page)
        assert state == "closed"
        # Reopening is a step-up action with a recorded reason, then re-close.
        # Without a fresh verification it is refused and the drawer stays shut.
        _reopen(page, revision)
        expect(page.get_by_text(gettext(STEP_UP_COPY))).to_be_visible()
        assert _drawer(page) == ("closed", revision)
        _step_up(page, reference, "finance_a")
        _finance(page, renewal_base_url, clinic)
        _reopen(page, revision)
        state, revision = _drawer(page)
        assert state != "closed", state
        _close(page, "0,00", revision)
        assert _drawer(page)[0] == "closed"
        capture(page, renewal_artifact_root, "journey-financial-close", "closed")
        lines = _export_balanced(page, renewal_base_url, clinic)
    finally:
        context.close()
    desk = staff_context(browser, 375)
    try:
        page = desk.new_page()
        # A nurse holds no financial permission: no financial action is offered.
        sign_in(page, renewal_base_url, reference, "nurse_a6")
        goto_settled(page, f"{renewal_base_url}/billing/clinics/{clinic}/finance/")
        expect(page.locator('#finance-content input[name="action"]')).to_have_count(0)
    finally:
        desk.close()
    flagship.cover(
        "J6.close-reopen",
        "failure",
        mismatch_refused=True,
        reopen_without_step_up_refused=True,
        reopened_with_reason=True,
        export_lines=lines,
        balanced=True,
    )
    flagship.cover("J6.fiscal-insurer", "blocked", nfse="EG-9", tiss="EG-8")
