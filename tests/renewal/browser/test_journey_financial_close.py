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

from typing import TYPE_CHECKING, Final

import psycopg
from django.utils.translation import gettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

import renewal.browser._j6_close as j6
import renewal.browser.test_billing as billing
import renewal.browser.test_billing_psp as psp
import renewal.browser.test_reports as reports
from renewal.browser._fixture_secrets import new_totp_key
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._totp_devices import reset_totp_device
from renewal.browser.flagship_support import (
    capture,
    run_fixture,
    sign_in,
    staff_context,
)
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_encounter import press

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

BLOCKED_COPY: Final = (
    "Refund and payout sending is a local rehearsal. No real money is moved. "
    "PSP and live legs remain BLOCKED-ON-EG."
)


def test_j6_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("journal", "real", "double-entry operational journal (todo 56)")
    flagship.adapter("psp", "synthetic", "recorded-psp-v1 fixture provider")
    flagship.adapter("pix", "synthetic", "synthetic-pix-v1 non-payable code")
    flagship.adapter("nfse", "synthetic", "local fiscal rehearsal, no fiscal validity")
    flagship.adapter("tiss", "synthetic", "XSD-validated fixture return, separate cash")
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


def test_j6_day_close_reopen_and_balanced_export(  # noqa: PLR0915 - one close procedure
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference, base = reference_clinic, renewal_base_url
    browser = renewal_page.context.browser
    assert browser is not None
    clinic = reference.unit_a
    physician = reference.personas["physician_a1"].user_id
    payer = billing.seed_patient(
        {
            "dsn": reference.dsn,
            "organization": reference.organization,
            "clinic_a": clinic,
            "receptionist_id": reference.personas["reception_a1"].user_id,
        },
        "Paciente Sint\u00e9tica Fechamento J6",
    )
    owner = staff_context(browser, 1280)
    try:
        page = owner.new_page()
        sign_in(page, base, reference, "org_admin")
        j6.finance(page, base, clinic)
        # The book is organization finance policy: it exists before any money.
        if page.locator('input[name="action"][value="create_book"]').count():
            j6.command(page, "create_book", {})
        # The practitioner rule is organization finance policy too.
        j6.command(
            page,
            "create_rule",
            {
                "practitioner_id": physician,
                "kind": "percent",
                "rule_value": str(j6.RULE_BASIS_POINTS),
            },
        )
        j6.recorded(page)
    finally:
        owner.close()
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in(page, base, reference, "finance_a")
        j6.finance(page, base, clinic)
        expect(page.get_by_text(gettext(BLOCKED_COPY))).to_be_visible()
        # Invoice: a real charge and its frozen terms with the policy rule.
        charge_url = billing.create_charge(
            page,
            f"{base}/billing/clinics/{clinic}/charges/",
            payer["patient"],
            j6.reais(j6.CHARGE),
        )
        invoice = billing.invoice_id_of(charge_url)
        # Issued and settled by attested confirmation: no payment code is
        # needed, and the finance role may not generate one.
        press(page, "issue")
        billing.expect_state(page, "issued")
        j6.finance(page, base, clinic)
        j6.command(
            page,
            "prepare_terms",
            {
                "invoice_id": invoice,
                "payer": "patient",
                "discount": "0,00",
                "adjustment": "0,00",
                # Option zero explicitly means no practitioner rule.
                "rule_id": 1,
            },
        )
        j6.recorded(page)
        j6.command(
            page,
            "complete_service",
            {
                "invoice_id": invoice,
                "evidence_reference": j6.evidence(),
                "attested": True,
            },
        )
        j6.recorded(page)
        # Settlement: confirmed on the charge screen, bridged into the journal.
        goto_settled(page, charge_url)
        billing.confirm_settlement(page, j6.reais(j6.CHARGE))
        j6.finance(page, base, clinic)
        expect(page.locator('tr[data-collection-state="posted"]')).to_have_count(1)
        # Fee, refund and payout on that same collection and practitioner.
        j6.command(
            page, "psp_fee", {"amount": j6.reais(j6.FEE), "account": "psp_clearing"}
        )
        j6.recorded(page)
        j6.command(page, "open_cash", {"counted": "0,00"})
        j6.command(
            page,
            "request_refund",
            {
                "allocation_id": 0,
                "amount": j6.reais(j6.REFUND),
                "refund_kind": "refund",
            },
        )
        j6.recorded(page)
        j6.command(
            page,
            "request_payout",
            {"practitioner_id": physician, "amount": j6.reais(j6.PAYOUT)},
        )
        j6.recorded(page)
        # Outgoing money needs a fresh verification: refused, then stepped up.
        j6.command(page, "approve_refund", {"refund_id": 0})
        j6.needs_step_up(page)
        _step_up(page, reference, "finance_a")
        j6.finance(page, base, clinic)
        j6.command(page, "approve_refund", {"refund_id": 0})
        j6.recorded(page)
        j6.command(page, "approve_payout", {"payout_id": 0})
        j6.recorded(page)
        for outcome in ("sending", "unknown"):
            j6.command(
                page,
                "refund_state",
                {"refund_id": 0, "outcome": outcome, "attested": True},
            )
            j6.recorded(page)
        # Exception: an unknown refund outcome keeps the day open.
        state, revision = j6.drawer(page)
        assert state == "open"
        _close(page, "0,00", revision)
        expect(page.locator('[role="alert"]').first).to_be_visible()
        assert j6.drawer(page)[0] == "open"
        capture(page, renewal_artifact_root, "journey-financial-close", "exception")
        j6.finance(page, base, clinic)
        j6.command(
            page,
            "refund_state",
            {
                "refund_id": 0,
                "outcome": "confirmed",
                "outcome_evidence": j6.evidence(),
                "attested": True,
            },
        )
        j6.recorded(page)
        for outcome in ("sending", "confirmed"):
            fields: dict[str, str | int | bool] = {
                "payout_id": 0,
                "outcome": outcome,
                "attested": True,
            }
            if outcome == "confirmed":
                fields["outcome_evidence"] = j6.evidence()
            j6.command(page, "payout_state", fields)
            j6.recorded(page)
        # No insurance operator screen exists. Its real services retain the
        # claim and return and allocate independently verified cash in this
        # same book. Fiscal authorization must not itself post any money.
        documents = run_fixture(
            reference.dsn,
            "financial-documents",
            {
                "organization": reference.organization,
                "clinic": clinic,
                "actor": reference.personas["finance_a"].user_id,
                "physician": physician,
                "receptionist": reference.personas["reception_a1"].user_id,
                "patient": payer["patient"],
                "enrollment": payer["enrollment"],
                "invoice": invoice,
            },
        )
        assert documents == {
            "fiscal_authorized": 1,
            "insurer_charged_minor": 12345,
            "insurer_collected_minor": 8345,
            "insurer_outstanding_minor": 4000,
            "return_replay_idempotent": True,
            "payment_replay_idempotent": True,
        }
        j6.finance(page, base, clinic)
        expect(
            page.locator(
                'tr[data-fiscal-state="authorized"][data-fiscal-rehearsal="true"]'
            )
        ).to_have_count(1)
        # Failure branch: a count that disagrees with the journal never closes.
        _close(page, "10,00", j6.drawer(page)[1])
        expect(page.locator('[role="alert"]').first).to_be_visible()
        assert j6.drawer(page)[0] == "open"
        capture(page, renewal_artifact_root, "journey-financial-close", "mismatch")
        j6.finance(page, base, clinic)
        _close(page, "0,00", j6.drawer(page)[1])
        state, revision = j6.drawer(page)
        assert state == "closed"
        # An auditable reopening with a recorded reason, then the re-close.
        _reopen(page, revision)
        state, revision = j6.drawer(page)
        assert state != "closed", state
        _close(page, "0,00", revision)
        assert j6.drawer(page)[0] == "closed"
        capture(page, renewal_artifact_root, "journey-financial-close", "closed")
        lines, accounts = j6.journal_totals(page, base, clinic)
    finally:
        context.close()
    # The same close reconciles every named fact to the centavo.
    repasse = j6.CHARGE * j6.RULE_BASIS_POINTS // 10_000
    clawback = j6.REFUND * j6.RULE_BASIS_POINTS // 10_000
    assert accounts.get("patient_ar", 0) == 0, accounts
    assert accounts.get("psp_fees") == j6.FEE, accounts
    assert accounts.get("contra_revenue") == j6.REFUND, accounts
    assert accounts.get("insurer_ar") == 4000, accounts
    assert accounts.get("practitioner_payables") == -(repasse - clawback - j6.PAYOUT), (
        accounts
    )
    desk = staff_context(browser, 375)
    try:
        page = desk.new_page()
        # A nurse holds no financial permission: no financial action is offered.
        sign_in(page, base, reference, "nurse_a6")
        goto_settled(page, f"{base}/billing/clinics/{clinic}/finance/")
        expect(page.locator('#finance-content input[name="action"]')).to_have_count(0)
    finally:
        desk.close()
    flagship.cover(
        "J6.close-reopen",
        "failure",
        unknown_outcome_blocked_close=True,
        mismatch_refused=True,
        approval_without_step_up_refused=True,
        reopened_with_reason=True,
        export_lines=lines,
        balanced=True,
    )
    flagship.cover(
        "J6.reconciled-facts",
        "happy",
        invoice=2,
        settlement=2,
        fee=1,
        refund=1,
        payout=1,
        reconciled_accounts=5,
    )
    flagship.cover(
        "J6.fiscal-insurer",
        "happy",
        surface="runtime-services-and-finance-screen",
        **documents,
    )
    flagship.cover("J6.fiscal-insurer", "blocked", nfse="EG-9", tiss="EG-8")
