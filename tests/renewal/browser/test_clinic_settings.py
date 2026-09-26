"""Native settings forms, real clinic_app serving, scoped branding and denial."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from playwright.sync_api import expect

from renewal.browser.engines import zoom_200
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_retention import post_action, seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)


def submit(page: Page, action: str) -> None:
    with page.expect_navigation():
        page.locator(f'button[value="{action}"]').click()


def capture(page: Page, root: Path, scene: str, width: int) -> None:
    destination = root / "clinic-settings"
    destination.mkdir(exist_ok=True)
    page.screenshot(path=str(destination / f"{scene}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def contrast(page: Page) -> float:
    return float(
        page.locator("[data-clinic-brand]").evaluate("""element => {
      const luminance = value => {
        const channels = value.match(/[\\d.]+/g).slice(0, 3).map(Number).map(n => {
          n /= 255; return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
      };
      const style = getComputedStyle(element);
      return (luminance(style.backgroundColor) + .05) / (luminance(style.color) + .05);
    }""")
    )


def publish_overlays(page: Page, width: int) -> None:
    form = page.locator('[data-settings-form="specialty"]')
    form.locator("#id_key").fill(f"sintetico-{width}")
    form.locator("#id_title").fill("Modelo sintético")
    for field in ("subjective", "objective", "assessment", "plan"):
        form.locator(f"#id_{field}").fill("Orientação sintética")
    submit(page, "specialty")
    expect(
        page.locator('[data-specialty-version="1"]').filter(
            has_text=f"sintetico-{width}"
        )
    ).to_be_visible()
    page.locator("#id_purpose").select_option("teleconsultation")
    page.locator("#id_text").fill("Texto sintético de consentimento.")
    submit(page, "consent")
    expect(page.locator("[data-consent-version]").first).to_be_visible()


def rejected_changes(page: Page, url: str, version: int) -> None:
    values = {
        "action": "settings",
        "expected_version": str(version),
        "display_name": "Safe",
        "brand_token": "teal",
        "reminder_hours": "6",
    }
    for extra in (
        {"timezone": "UTC"},
        {"css": "display:none"},
        {"brand_token": "#ffffff"},
        {"display_name": "<script>alert(1)</script>"},
    ):
        assert post_action(page, url, {**values, **extra}) == 400
    page.locator("#id_display_name").fill("body {display:none}")
    with page.expect_response(lambda r: r.request.method == "POST") as rejected:
        submit(page, "settings")
    assert rejected.value.status == 400
    expect(page.locator('[data-settings-form="settings"] .errorlist')).to_be_visible()


def publish_brand(page: Page, width: int) -> int:
    version = int(page.locator("#id_expected_version").input_value())
    page.locator("#id_display_name").fill(f"Marca Sintética {width}")
    page.locator("#id_contact_email").fill("contato@example.invalid")
    page.locator("#id_contact_phone").fill("+5511999990000")
    page.locator("#id_brand_token").select_option("navy" if width == 768 else "teal")
    page.locator("#id_reminder_hours").select_option("6")
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="PNG")
    page.locator("#id_logo").set_input_files(
        {"name": "logo.png", "mimeType": "image/png", "buffer": image.getvalue()}
    )
    submit(page, "settings")
    expect(page.locator("[data-configuration-version]")).to_have_text(str(version + 1))
    expect(page.locator("[data-clinic-brand] strong")).to_have_text(
        f"Marca Sintética {width}"
    )
    assert contrast(page) >= 4.5
    assert page.locator("[data-clinic-brand] img").evaluate(
        "e => e.complete && e.naturalWidth > 0"
    )
    return version + 1


def seed_settings_manager(staff: dict[str, str]) -> dict[str, str]:
    manager = seed_manager(staff)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id,user_id,organization_id,clinic_id,role) "
            "VALUES (%s,%s,%s,%s,'clinic_admin')",
            [str(uuid4()), manager["id"], staff["organization"], staff["clinic_b"]],
        )
    return manager


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_settings_native_publication_isolation_and_rejection(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    manager = seed_settings_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False, viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    sign_in_manager(page, renewal_base_url, staff, manager)
    url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/"
    other = f"{renewal_base_url}/clinics/{staff['clinic_b']}/settings/"
    page.goto(other)
    other_name = page.locator("#id_display_name").input_value()
    expect(page.locator("[data-configuration-version]")).to_have_text("0")
    page.goto(url)
    version = publish_brand(page, width)
    capture(page, renewal_artifact_root, "published", width)
    page.reload()
    expect(page.locator("[data-configuration-version]")).to_have_text(str(version))
    publish_overlays(page, width)
    capture(page, renewal_artifact_root, "overlays", width)
    rejected_changes(page, url, version)
    capture(page, renewal_artifact_root, "rejected", width)
    page.goto(other)
    expect(page.locator("#id_display_name")).to_have_value(other_name)
    expect(page.locator("[data-configuration-version]")).to_have_text("0")
    expect(page.locator("[data-clinic-brand]")).to_have_count(0)
    capture(page, renewal_artifact_root, "other-clinic-unchanged", width)
    page.goto(url)
    if width == 375:
        page.set_viewport_size({"width": 320, "height": 900})
        capture(page, renewal_artifact_root, "reflow", 320)
        page.emulate_media(forced_colors="active", reduced_motion="reduce")
        capture(page, renewal_artifact_root, "forced-colors", 320)
        page.emulate_media(forced_colors="none")
        zoom_context, zoomed = zoom_200(page)
        zoomed.goto(url)
        assert zoomed.evaluate("[devicePixelRatio, innerWidth]") == [2, 640]
        capture(zoomed, renewal_artifact_root, "zoom-200-layout", 640)
        zoom_context.close()
    context.close()
    physician_context = browser.new_context(viewport={"width": width, "height": 900})
    physician = physician_context.new_page()
    _sign_in_physician(physician, renewal_base_url, staff)
    response = physician.goto(url)
    assert response is not None
    assert response.status == 403
    expect(physician.locator('[data-module="settings"]')).to_have_count(0)
    capture(physician, renewal_artifact_root, "physician-denied", width)
    physician_context.close()
