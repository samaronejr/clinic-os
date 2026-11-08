"""Recorded PIX/card instructions and sealed receipts on the real patient routes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser.a11y_support import check_page
from renewal.browser.test_availability import availability_staff
from renewal.browser.test_billing import (
    capture,
    expect_state,
    seed_patient,
)
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_retention import seed_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
_run_fixture = subprocess.run


def fixture(staff: dict[str, str], **data: str) -> dict[str, str]:
    """Run production domain services with explicit synthetic owner setup."""
    result = _run_fixture(
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from renewal.psp_fixture import main; main()",
        ],
        input=json.dumps(
            {
                "organization": staff["organization"],
                "clinic": staff["clinic_a"],
                **data,
            }
        ),
        env={
            **os.environ,
            "APP_DATABASE_URL": staff["dsn"],
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
            "BILLING_SYNTHETIC_PSP": "true",
            "PYTHONPATH": ".:tests",
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    receipt: dict[str, str] = json.loads(result.stdout)
    return receipt


@pytest.mark.parametrize(
    "case",
    [
        ("pix", 1280, True),
        ("card", 1280, True),
        ("pix", 375, False),
        ("card", 375, False),
    ],
)
def test_recorded_psp_instructions_never_pay_until_a_sealed_receipt(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    case: tuple[str, int, bool],
) -> None:
    method, width, javascript = case
    staff = availability_staff
    manager = seed_manager(staff)
    payer = seed_patient(staff, f"Paciente Sintetico PSP {method} {width}")
    prepared = fixture(
        staff,
        action="prepare",
        actor=manager["id"],
        patient=payer["patient"],
        method=method,
    )
    intent_key = prepared["intent"].replace("-", "")
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(
        viewport={"width": width, "height": 900},
        locale="pt-BR",
        java_script_enabled=javascript,
    ) as context:
        page = context.new_page()
        _redeem(page, renewal_base_url, staff["clinic_a"], payer["code"])
        click_to_navigate(page.locator("#charges-link"))
        page.locator("[data-charge] a").click()
        expect_state(page, "pending")
        instructions = page.locator("[data-psp-instructions]")
        expect(instructions).to_contain_text(
            gettext(
                "Recorded sandbox rehearsal. No real payment or card data is collected."
            )
        )
        if method == "pix":
            expect(page.locator("#recorded-psp-code")).to_have_value(
                f"SYNTHETIC-NOT-PAYABLE|fixture-{intent_key}|18000|BRL"
            )
        else:
            expect(instructions.get_by_role("link")).to_have_attribute(
                "href", f"https://payments.invalid/{intent_key}"
            )
            expect(instructions).to_contain_text(
                gettext("Installments agreed by the clinic: %(count)s.") % {"count": 3}
            )
        state = f"psp-{method}-pending"
        capture(page, renewal_artifact_root, state, width)
        if javascript:
            check_page(page, renewal_base_url, renewal_artifact_root, state)
        fixture(staff, action="settle", actor=manager["id"], intent=prepared["intent"])
        goto_settled(page, f"{renewal_base_url}/patient/charges/{prepared['invoice']}/")
        expect_state(page, "paid")
        expect(page.locator("[data-psp-instructions]")).to_have_count(0)
        expect(page.locator("[data-receipt]")).to_be_visible()
        capture(page, renewal_artifact_root, f"psp-{method}-paid", width)
