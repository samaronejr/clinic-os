"""Plan item 30 QA within the registered patient-workspace suite."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.patient_workspace_support import submit as _submit
from renewal.browser.patient_workspace_support import tab as _tab
from renewal.browser.test_availability import _sign_in_physician
from renewal.browser.test_retention import (
    post_action,
    seed_manager,
    sign_in_manager,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page


def submit_pack(page: Page, button: Locator, *, status: int = 200) -> None:
    """Subscribe to the exact response and swap before triggering the form."""
    token = uuid4().hex
    page.evaluate(
        """token => {
          const settled = event => {
            if (event.detail.target?.id !== "main-content") return;
            document.documentElement.dataset.packSettled = token;
            document.removeEventListener("htmx:afterSettle", settled);
          };
          document.addEventListener("htmx:afterSettle", settled);
        }""",
        token,
    )
    url = page.url
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as pending:
        button.click()
    assert pending.value.status == status
    expect(page.locator("html")).to_have_attribute("data-pack-settled", token)


@pytest.fixture
def installed_pediatrics(
    renewal_page: Page,
    renewal_base_url: str,
    specialty_patient: tuple[dict[str, str], dict[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    page = renewal_page
    staff, data = specialty_patient
    manager = seed_manager(staff)
    page.context.clear_cookies()
    sign_in_manager(page, renewal_base_url, staff, manager)
    url = f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/specialty-packs/"
    goto_settled(page, url)
    pediatrics = page.locator('[data-pack="pediatrics"]')
    if pediatrics.locator('button[value="install"]').count():
        submit_pack(page, pediatrics.locator('button[value="install"]'))
    expect(pediatrics.locator('button[value="uninstall"]')).to_be_visible()
    return staff, data


@pytest.mark.parametrize("specialty_patient", ["2038-07-30"], indirect=True)
def test_specialty_pack_growth_license_and_decimal_refusal(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    installed_pediatrics: tuple[dict[str, str], dict[str, str]],
    browser_report: dict[str, object],
) -> None:
    page = renewal_page
    staff, data = installed_pediatrics
    url = f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/specialty-packs/"
    pending = page.locator('[data-calculator="growth_percentile"]')
    pending.locator("summary").click()
    expect(pending).to_contain_text(gettext("Unavailable: license pending"))
    folder = renewal_artifact_root / "specialty-packs"
    folder.mkdir(mode=0o700, exist_ok=True)
    full_page_screenshot(page, folder / "failure-license.png")
    check_page(page, renewal_base_url, renewal_artifact_root, "pack-license")
    submit_pack(page, page.locator('[data-pack="general"] button[value="install"]'))
    page.locator('[data-calculator="bmi"] summary').click()
    page.locator('[name="bmi-weight"]').fill("1,234.5")
    page.locator('[name="bmi-weight_unit"]').select_option("kg")
    page.locator('[name="bmi-height"]').fill("175")
    page.locator('[name="bmi-height_unit"]').select_option("cm")
    submit_pack(
        page,
        page.locator('[data-calculator="bmi"] button[value="calculate"]'),
        status=400,
    )
    expect(page.locator('[data-calculator="bmi"] [data-focus-error]')).to_be_visible()
    expect(page.locator("[data-calculation-result]")).to_have_count(0)
    full_page_screenshot(page, folder / "adversarial-decimal.png")
    page.locator('[name="bmi-weight"]').fill("70")
    submit_pack(page, page.locator('[data-calculator="bmi"] button[value="calculate"]'))
    expect(page.locator("[data-calculation-result]")).to_contain_text("22.86")
    full_page_screenshot(page, folder / "calculation-success.png")
    assert (
        post_action(
            page,
            url,
            {
                "action": "calculate",
                "pack_key": "general",
                "calculator_key": "__import__",
                "version": "1",
            },
        )
        == 403
    )
    page.context.clear_cookies()
    _sign_in_physician(page, renewal_base_url, staff)
    workspace = f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/patient-workspace/"
    assert (
        post_action(
            page, workspace, {"action": "open", "enrollment_id": data["enrollment"]}
        )
        == 200
    )
    goto_settled(page, workspace)
    _tab(page, "vitals")
    page.locator('[name="vitals-weight"]').fill("12,5")
    page.locator('[name="vitals-height"]').fill("90")
    _submit(page, page.locator('button[name="action"][value="vitals"]'))
    expect(page.locator('[data-observation-kind="weight"]')).to_contain_text("12,5")
    expect(page.locator('[data-observation-kind="height"]')).to_contain_text("90")
    full_page_screenshot(page, folder / "pack.png")
    check_page(page, renewal_base_url, renewal_artifact_root, "pack-growth")
    page.set_viewport_size({"width": 390, "height": 844})
    full_page_screenshot(page, folder / "pack-phone.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 1000})
    browser_report["checks"] = [
        "pediatrics-install",
        "growth-recording-without-percentiles",
        "missing-license-visible",
        "decimal-confusion-refused",
        "unknown-code-refused",
        "phone-reflow",
    ]


@pytest.mark.parametrize("specialty_patient", ["2038-07-31"], indirect=True)
def test_specialty_immunization_uses_a_real_open_encounter(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    installed_pediatrics: tuple[dict[str, str], dict[str, str]],
    browser_report: dict[str, object],
) -> None:
    page = renewal_page
    staff, data = installed_pediatrics
    page.context.clear_cookies()
    _sign_in_physician(page, renewal_base_url, staff)
    assert (
        post_action(
            page,
            f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/encounter/",
            {"action": "open", "appointment_id": data["appointment"]},
        )
        == 302
    )
    workspace = f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/patient-workspace/"
    assert (
        post_action(
            page, workspace, {"action": "open", "enrollment_id": data["enrollment"]}
        )
        == 200
    )
    goto_settled(page, workspace)
    _tab(page, "specialty")
    page.locator('[name="immunization-vaccine"]').fill("Vacina sintética")
    page.locator('[name="immunization-administered_on"]').fill("02/01/2020")
    page.locator('[name="immunization-dose"]').fill("Dose sintética")
    page.locator('[name="immunization-provenance"]').select_option(
        "reported_by_patient"
    )
    identifier = page.locator(
        '[name="immunization-encounter_id"] option'
    ).last.get_attribute("value")
    assert identifier
    page.locator('[name="immunization-encounter_id"]').select_option(identifier)
    _submit(page, page.locator('button[value="immunization"]'))
    expect(page.locator("article.patient-workspace-row")).to_contain_text(
        "Vacina sintética"
    )
    folder = renewal_artifact_root / "specialty-packs"
    folder.mkdir(mode=0o700, exist_ok=True)
    full_page_screenshot(page, folder / "immunization.png")
    browser_report["checks"] = ["real-open-encounter", "immunization-native-post"]


@pytest.fixture
def filled_bmi(
    renewal_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> Locator:
    page = renewal_page
    page.context.clear_cookies()
    manager = seed_manager(availability_staff)
    sign_in_manager(page, renewal_base_url, availability_staff, manager)
    url = (
        f"{renewal_base_url}/ehr/clinics/"
        f"{availability_staff['clinic_a']}/specialty-packs/"
    )
    goto_settled(page, url)
    general = page.locator('[data-pack="general"]')
    if general.locator('button[value="install"]').count():
        submit_pack(page, general.locator('button[value="install"]'))
    calculator = page.locator('[data-calculator="bmi"]')
    calculator.locator("summary").click()
    calculator.locator('[name="bmi-weight"]').fill("70")
    calculator.locator('[name="bmi-weight_unit"]').select_option("kg")
    calculator.locator('[name="bmi-height"]').fill("175")
    calculator.locator('[name="bmi-height_unit"]').select_option("cm")
    return calculator


def test_specialty_pack_native_and_responsive_controls(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    filled_bmi: Locator,
    browser_report: dict[str, object],
) -> None:
    page = renewal_page
    folder = renewal_artifact_root / "specialty-packs"
    folder.mkdir(mode=0o700, exist_ok=True)
    for theme, width in (
        ("light", 320),
        ("light", 1280),
        ("dark", 375),
        ("dark", 1280),
    ):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for control in filled_bmi.locator(
            "summary, input:not([type=hidden]), select, button"
        ).all():
            if control.is_visible():
                bounds = control.bounding_box()
                assert bounds
                assert bounds["width"] >= 44
                assert bounds["height"] >= 44
        full_page_screenshot(page, folder / f"calculator-{theme}-{width}.png")
        check_page(
            page, renewal_base_url, renewal_artifact_root, f"calculator-{theme}-{width}"
        )
    browser = page.context.browser
    assert browser
    with browser.new_context(
        java_script_enabled=False, storage_state=page.context.storage_state()
    ) as native:
        native_page = native.new_page()
        goto_settled(native_page, page.url)
        calculator = native_page.locator('[data-calculator="bmi"]')
        calculator.locator("summary").click()
        calculator.locator('[name="bmi-weight"]').fill("70")
        calculator.locator('[name="bmi-weight_unit"]').select_option("kg")
        calculator.locator('[name="bmi-height"]').fill("175")
        calculator.locator('[name="bmi-height_unit"]').select_option("cm")
        click_to_navigate(calculator.locator('button[value="calculate"]'))
        expect(native_page.locator("[data-calculation-result]")).to_contain_text(
            "22.86"
        )
        full_page_screenshot(native_page, folder / "native-calculation.png")
    browser_report["checks"] = ["native-no-js-calculation", "light-dark-phone-desktop"]


def test_specialty_pack_offline_and_denial_states(
    renewal_page: Page,
    renewal_artifact_root: Path,
    filled_bmi: Locator,
    browser_report: dict[str, object],
) -> None:
    page = renewal_page
    folder = renewal_artifact_root / "specialty-packs"
    folder.mkdir(mode=0o700, exist_ok=True)
    url = page.url
    page.context.set_offline(True)
    try:
        with page.expect_event(
            "requestfailed",
            predicate=lambda request: request.url == url and request.method == "POST",
        ):
            filled_bmi.locator('button[value="calculate"]').click()
        expect(filled_bmi.locator("[data-network-error]")).to_be_visible()
        expect(filled_bmi.locator('[name="bmi-weight"]')).to_have_value("70")
        expect(page.locator("[data-calculation-result]")).to_have_count(0)
        full_page_screenshot(page, folder / "offline-retained-inputs.png")
    finally:
        page.context.set_offline(False)
    filled_bmi.locator('[name="calculator_key"]').evaluate(
        "node => node.value = '__import__'"
    )
    submit_pack(page, filled_bmi.locator('button[value="calculate"]'), status=403)
    expect(page.locator("main h1")).to_contain_text(gettext("Access unavailable"))
    expect(filled_bmi).to_have_count(0)
    full_page_screenshot(page, folder / "denial.png")
    browser_report["checks"] = ["offline-inputs-retained", "denial-visible-no-result"]
