"""Patient workspace on real clinic_app: vitals, provenance and native forms."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser._navigation import goto_settled
from renewal.browser._protected import encrypt
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.patient_workspace_support import submit as _submit
from renewal.browser.patient_workspace_support import tab as _tab
from renewal.browser.specialty_accessibility import (
    test_specialty_pack_keyboard_announces_refusal_and_result,
    test_specialty_pack_zoom_and_reduced_motion,
)
from renewal.browser.specialty_packs_scenario import (
    filled_bmi,
    installed_pediatrics,
    test_specialty_immunization_uses_a_real_open_encounter,
    test_specialty_pack_growth_license_and_decimal_refusal,
    test_specialty_pack_native_and_responsive_controls,
    test_specialty_pack_offline_and_denial_states,
)
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_encounter import seed
from renewal.browser.test_retention import post_action

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = (
    "availability_staff",
    "filled_bmi",
    "installed_pediatrics",
    "test_specialty_immunization_uses_a_real_open_encounter",
    "test_specialty_pack_growth_license_and_decimal_refusal",
    "test_specialty_pack_keyboard_announces_refusal_and_result",
    "test_specialty_pack_native_and_responsive_controls",
    "test_specialty_pack_offline_and_denial_states",
    "test_specialty_pack_zoom_and_reduced_motion",
)


def _patient(staff: dict[str, str], day: str) -> dict[str, str]:
    data = seed(staff, day)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_professionalregistration "
            "(id,organization_id,clinic_id,user_id,role,council,number,jurisdiction,"
            "synthetic,status,valid_from,valid_to) "
            "SELECT %s,%s,c.id,%s,'physician','CRM',%s,c.crm_uf,true,'regular',"
            "now()-interval '1 day',now()+interval '1 day' "
            "FROM clinic_app.identity_clinic c WHERE c.id=%s "
            "AND NOT EXISTS (SELECT 1 "
            "FROM clinic_app.identity_professionalregistration r "
            "WHERE r.clinic_id=c.id AND r.user_id=%s AND r.role='physician')",
            [
                str(uuid4()),
                staff["organization"],
                staff["physician_a_id"],
                encrypt(conn, "identity.registration.number", b"SINTETICO-0028"),
                staff["clinic_a"],
                staff["physician_a_id"],
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_careteammembership "
            "(id,organization_id,clinic_id,patient_enrollment_id,user_id,role,"
            "valid_from,valid_to,revoked_at) "
            "VALUES (%s,%s,%s,%s,%s,'physician',now()-interval '1 day',"
            "now()+interval '1 day',NULL)",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                data["enrollment"],
                staff["physician_a_id"],
            ],
        )
    return data


@pytest.fixture
def specialty_patient(
    availability_staff: dict[str, str],
    request: pytest.FixtureRequest,
) -> tuple[dict[str, str], dict[str, str]]:
    return availability_staff, _patient(availability_staff, str(request.param))


def _capture(page: Page, root: Path, state: str) -> None:
    folder = root / "patient-workspace"
    folder.mkdir(mode=0o700, exist_ok=True)
    expect(page.locator('#patient-workspace[aria-busy="true"]')).to_have_count(0)
    field = page.locator('[name="vitals-temperature"]')
    if field.count():
        expect(field).to_be_enabled()
    page.evaluate(
        # A cancelled CSS transition is settled too, not an application error.
        "() => Promise.allSettled(document.getAnimations()"
        ".map(animation => animation.finished))"
    )
    full_page_screenshot(page, folder / f"{state}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _medication_review(page: Page) -> None:
    expect(page.locator('[name="medication-status"]')).to_have_value("unknown")
    for field, value in {
        "text": "Medicamento sintético 28",
        "dose_value": "5,0",
        "times_per_day": "2",
    }.items():
        page.locator(f'[name="medication-{field}"]').fill(value)
    page.locator('[name="medication-dose_unit"]').select_option("mg")
    page.locator('[name="medication-route"]').select_option("oral")
    _submit(page, page.locator('button[name="action"][value="medication"]'))
    row = page.locator("article.patient-workspace-row").filter(
        has_text="Medicamento sintético 28"
    )
    expect(row.locator('[data-provenance="reported"]')).to_be_visible()
    row.locator('[name="reviewed"]').check()
    row.locator('[name="status"]').select_option("active")
    _submit(page, row.locator('button[name="action"][value="reconcile"]'))
    expect(row.locator('[data-provenance="confirmed"]')).to_be_visible()


def _vitals(page: Page, artifact_root: Path) -> None:
    _tab(page, "vitals")
    _capture(page, artifact_root, "vitals-empty")
    for name, value in {
        "temperature": "37,5",
        "weight": "70,0",
        "height": "175",
        "systolic_bp": "120",
        "diastolic_bp": "80",
        "heart_rate": "70",
        "respiratory_rate": "16",
        "spo2": "98",
    }.items():
        page.locator(f'[name="vitals-{name}"]').fill(value)
    _submit(page, page.locator('button[value="vitals"][name="action"]'))
    expect(page.locator('[data-observation-kind="temperature"]')).to_contain_text(
        "37,5"
    )
    expect(page.locator('[data-observation-kind="bmi"]')).to_contain_text("22,86")
    expect(page.locator("#vitals-trend .chart-svg")).to_be_visible()
    expect(page.locator('[name="vitals-temperature"]')).to_be_enabled()
    page.locator("#vitals-trend details").evaluate("(node) => { node.open = true; }")
    expect(page.locator("#vitals-trend table")).to_contain_text("37,5")
    _capture(page, artifact_root, "vitals-desktop-light")
    page.set_viewport_size({"width": 390, "height": 844})
    _capture(page, artifact_root, "vitals-phone-light")
    page.locator("html").evaluate("(node) => node.setAttribute('data-theme', 'dark')")
    _capture(page, artifact_root, "vitals-phone-dark")
    page.set_viewport_size({"width": 1440, "height": 1000})
    _capture(page, artifact_root, "vitals-desktop-dark")
    page.locator("html").evaluate("(node) => node.setAttribute('data-theme', 'light')")
    page.locator('[name="observation-kind"]').select_option("temperature")
    page.locator('[name="observation-value"]').fill("37,5")
    page.locator('[name="observation-unit"]').select_option("kg")
    _submit(
        page, page.locator('button[name="action"][value="observation"]'), status=400
    )
    expect(page.locator('[role="alert"][data-focus-error]').first).to_be_visible()
    assert page.locator('[data-observation-kind="temperature"]').count() == 1
    _capture(page, artifact_root, "invalid-unit")


def test_vitals_trend_invalid_unit_and_workspace_tabs(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page, staff = renewal_page, availability_staff
    data = _patient(staff, "2038-06-12")
    _sign_in_physician(page, renewal_base_url, staff)
    url = f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/patient-workspace/"
    assert (
        post_action(page, url, {"action": "open", "enrollment_id": data["enrollment"]})
        == 200
    )
    goto_settled(page, url)
    expect(page.locator("#patient-workspace-title")).to_be_visible()
    _vitals(page, renewal_artifact_root)
    check_page(page, renewal_base_url, renewal_artifact_root, "workspace-invalid-unit")
    for tab in (
        "medications",
        "timeline",
        "encounters",
        "documents",
        "forms",
        "overview",
    ):
        _tab(page, tab)
        expect(page.locator(f'button[name="tab"][value="{tab}"]')).to_have_attribute(
            "aria-current", "page"
        )
        _capture(page, renewal_artifact_root, f"{tab}-desktop")
        if tab == "medications":
            _medication_review(page)
            _capture(page, renewal_artifact_root, "medication-reconciled")
            check_page(
                page, renewal_base_url, renewal_artifact_root, "workspace-medications"
            )
    page.emulate_media(reduced_motion="reduce")
    for width in (640, 320):
        page.set_viewport_size({"width": width, "height": 900})
        _capture(page, renewal_artifact_root, f"reflow-{width}")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator("#patient-workspace-title").focus()
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as shortcut:
        page.keyboard.press("g")
        page.keyboard.press("t")
    assert shortcut.value.status == 200
    expect(page.locator('button[name="tab"][value="timeline"]')).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator("#patient-workspace-title")).to_be_focused()
    _capture(page, renewal_artifact_root, "keyboard-timeline")
    _tab(page, "overview")
    browser_report["checks"] = [
        "exact-decimal-comma",
        "deterministic-bmi",
        "trend-table-alternative",
        "invalid-unit-refused",
        "htmx-tab-navigation",
        "paired-theme-responsive",
        "reported-medication-explicit-review",
        "keyboard-sequence-focus",
        "reflow-320-640",
        "reduced-motion",
    ]
    check_page(
        page, renewal_base_url, renewal_artifact_root, "patient-workspace-overview"
    )


def test_workspace_native_without_javascript(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    staff = availability_staff
    data = _patient(staff, "2038-06-13")
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(java_script_enabled=False) as context:
        page = context.new_page()
        _sign_in_physician(page, renewal_base_url, staff)
        url = f"{renewal_base_url}/ehr/clinics/{staff['clinic_a']}/patient-workspace/"
        assert (
            post_action(
                page, url, {"action": "open", "enrollment_id": data["enrollment"]}
            )
            == 200
        )
        goto_settled(page, url)
        _tab(page, "vitals")
        page.locator('[name="vitals-temperature"]').fill("38,1")
        _submit(page, page.locator('button[name="action"][value="vitals"]'))
        expect(page.locator('[data-observation-kind="temperature"]')).to_contain_text(
            "38,1"
        )
        full_page_screenshot(page, renewal_artifact_root / "native-vitals.png")
    browser_report["checks"] = ["javascript-disabled", "native-post", "decimal-comma"]
