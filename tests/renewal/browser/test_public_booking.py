"""Todo 62: branded native booking, selector privacy and optionality."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser import a11y_support
from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
)
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_availability import availability_staff
from renewal.browser.test_self_booking import _seed

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
PATH = "/book/"
CODE = "synthetic-public-booking"


@pytest.fixture(scope="session", autouse=True)
def booking_profile(availability_staff: dict[str, str]) -> dict[str, str]:
    """Publish factual clinic content with a canonical manager role."""
    staff = availability_staff
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userclinicrole"
            "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version)"
            " VALUES(%s,%s,%s,%s,'clinic_manager',1)",
            [
                str(uuid4()),
                staff["receptionist_id"],
                staff["organization"],
                staff["clinic_a"],
            ],
        )
        conn.execute(
            "SELECT set_config('app.current_user_id',%s,true)",
            [staff["receptionist_id"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.crm_publicbookingprofile"
            "(id,organization_id,clinic_id,code,description,active,created_at)"
            " VALUES(%s,%s,%s,%s,%s,true,now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                CODE,
                "Agendamento de consultas pelo portal da clinica, "
                "com acesso verificado.",
            ],
        )
    return staff


def _select(page: Page, base_url: str) -> None:
    goto_settled(page, base_url + PATH)
    page.locator("#clinic-code").fill(CODE)
    click_to_navigate(page.get_by_role("button", name="Ver página da clínica"))
    assert page.url == base_url + PATH


def _capture(page: Page, root: Path, name: str) -> str:
    folder = root / "public-booking"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{name}.png"
    full_page_screenshot(page, path)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert (
        page.locator("a[href*='?'], a[href*='synthetic-public-booking']").count() == 0
    )
    return f"public-booking/{path.name}"


@pytest.mark.parametrize("scene", [(1280, 1), (375, 1), (640, 2), (320, 4)])
def test_public_desktop_mobile_keyboard_and_reflow(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
    scene: tuple[int, int],
) -> None:
    """Review the factual public surface at desktop, mobile and both zoom scenes."""
    width, scale = scene
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        device_scale_factor=scale,
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        goto_settled(page, renewal_base_url + PATH)
        a11y_support.check_page(
            page,
            renewal_base_url,
            renewal_artifact_root,
            f"public-booking-select-{width}-{scale}",
        )
        page.keyboard.press("Tab")
        expect(page.locator(".skip-link")).to_be_focused()
        expect(page.locator("#booking-main")).to_be_visible()
        page.locator("#clinic-code").focus()
        assert page.locator("#clinic-code").evaluate(
            "e => getComputedStyle(e).outlineStyle !== 'none'"
        )
        page.locator("#clinic-code").fill(CODE)
        page.keyboard.press("Tab")
        expect(page.get_by_role("button", name="Ver página da clínica")).to_be_focused()
        with expect_document(page):
            page.keyboard.press("Enter")
        expect(
            page.get_by_role("heading", name="Sua próxima consulta começa aqui.")
        ).to_be_visible()
        expect(
            page.get_by_role("link", name="Verificar acesso do paciente")
        ).to_be_visible()
        assert page.locator("[name=slot]").count() == 0
        targets = page.locator("button, .button, input")
        for index in range(targets.count()):
            box = targets.nth(index).bounding_box()
            if box:
                assert box["height"] >= 44
        screenshot = _capture(
            page, renewal_artifact_root, f"brand-{width}-scale-{scale}"
        )
        axe = a11y_support.check_page(
            page,
            renewal_base_url,
            renewal_artifact_root,
            f"public-booking-{width}-{scale}",
        )
        checks = browser_report["checks"]
        assert isinstance(checks, list)
        checks.append(
            {
                "scenario": "public-clinic",
                "width": width,
                "scale": scale,
                "capture_path": screenshot,
                "axe": axe,
                "keyboard": True,
                "no_url_selectors": True,
                "synthetic": True,
            }
        )
    finally:
        context.close()


def test_native_patient_booking_reuses_existing_service(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
    booking_profile: dict[str, str],
) -> None:
    """Book without JavaScript and with every selector confined to POST bodies."""
    data = _seed(booking_profile, "2031-08-12")
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False,
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1280, "height": 900},
    )
    try:
        page = context.new_page()
        _select(page, renewal_base_url)
        click_to_navigate(page.get_by_role("link", name="Verificar acesso do paciente"))
        page.locator("[name=code]").fill(data["code"])
        click_to_navigate(
            page.get_by_role("button", name="Verificar acesso do paciente")
        )
        goto_settled(page, renewal_base_url + PATH)
        page.locator("#booking-day").fill(data["day"])
        click_to_navigate(page.get_by_role("button", name="Ver horários disponíveis"))
        expect(page.locator(".crm-slot")).to_have_count(8)
        assert data["patient"] not in page.url
        assert data["enrollment"] not in page.url
        click_to_navigate(page.get_by_role("button", name="Agendar este horário").first)
        expect(page.get_by_role("status")).to_contain_text("Agendamento registrado")
        assert page.url == renewal_base_url + PATH
        screenshot = _capture(page, renewal_artifact_root, "native-booked")
        with psycopg.connect(booking_profile["dsn"]) as conn:
            conn.execute("SET ROLE clinic_app")
            conn.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [booking_profile["organization"]],
            )
            conn.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [booking_profile["receptionist_id"]],
            )
            assert conn.execute(
                "SELECT count(*) FROM clinic_app.scheduling_appointment "
                "WHERE patient_id=%s",
                [data["patient"]],
            ).fetchone() == (1,)
        checks = browser_report["checks"]
        assert isinstance(checks, list)
        checks.append(
            {
                "scenario": "native-booked",
                "capture_path": screenshot,
                "runtime_role": "clinic_app",
                "synthetic": True,
            }
        )
    finally:
        context.close()


def test_unknown_clinic_and_get_selectors_do_not_resolve(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    """Forged selection and GET selectors never disclose a patient or a clinic."""
    goto_settled(renewal_page, renewal_base_url + PATH)
    renewal_page.locator("#clinic-code").fill("unknown-clinic")
    with expect_document(renewal_page) as navigation:
        renewal_page.get_by_role("button", name="Ver página da clínica").click()
    assert navigation.value is not None
    assert navigation.value.status == 404
    expect(renewal_page.get_by_role("alert")).to_contain_text("Clínica indisponível")
    a11y_support.check_page(
        renewal_page, renewal_base_url, renewal_artifact_root, "public-booking-unknown"
    )
    response = goto_settled(
        renewal_page, renewal_base_url + PATH + "?enrollment=synthetic-sentinel"
    )
    assert response is not None
    assert response.status == 400
    assert "synthetic-sentinel" not in renewal_page.locator("body").inner_text()
    a11y_support.check_page(
        renewal_page,
        renewal_base_url,
        renewal_artifact_root,
        "public-booking-get-denied",
    )


def test_private_invitation_form_has_no_url_selector(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    """Review access form semantics without displaying or storing a private code."""
    _select(renewal_page, renewal_base_url)
    click_to_navigate(
        renewal_page.get_by_role("link", name="Verificar acesso do paciente")
    )
    assert renewal_page.url == renewal_base_url + "/book/access/"
    expect(renewal_page.locator("[name=code]")).to_have_attribute("type", "password")
    assert renewal_page.locator("input[name=clinic_id]").count() == 0
    a11y_support.check_page(
        renewal_page, renewal_base_url, renewal_artifact_root, "public-booking-access"
    )
    _capture(renewal_page, renewal_artifact_root, "private-access-empty")
