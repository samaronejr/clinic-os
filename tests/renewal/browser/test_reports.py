"""Governed Reports: real browser, native forms, axe, keyboard and reflow."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._page_wait import evaluate_js
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_encounter import seed
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page


def _complete_source(staff: dict[str, str]) -> None:
    data = seed(staff, "2035-06-02")
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [staff["organization"], staff["physician_a_id"]],
        )
        for state in ("in_progress", "completed"):
            conn.execute(
                "UPDATE clinic_app.scheduling_appointment SET status=%s WHERE id=%s",
                [state, data["appointment"]],
            )


def _native_refresh(page: Page) -> None:
    page.get_by_label(gettext("From"), exact=True).fill("02/06/2035")
    page.get_by_label(gettext("Through"), exact=True).fill("02/06/2035")
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator('[data-report-state="fresh"]')).to_be_visible()


def test_reports_sources_review_boundaries_and_accessibility(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    staff = availability_staff
    manager = seed_manager(staff)
    _complete_source(staff)
    root = renewal_artifact_root / "reports"
    root.mkdir(mode=0o700, exist_ok=True)
    url = f"{renewal_base_url}/reports/clinics/{staff['clinic_a']}/"
    with browser.new_context(
        viewport={"width": 1280, "height": 900}, locale="pt-BR"
    ) as context:
        page = context.new_page()
        sign_in_manager(page, renewal_base_url, staff, manager)
        goto_settled(page, url)
        expect(
            page.get_by_role("heading", name=gettext("Reports"), exact=True)
        ).to_be_visible()
        page.get_by_label(gettext("From"), exact=True).fill("02/06/2035")
        page.get_by_label(gettext("Through"), exact=True).fill("02/06/2035")
        with page.expect_response(
            lambda response: response.url == url and response.request.method == "POST"
        ):
            page.locator('button[value="refresh"]').click()
        expect(page.locator('[data-report-state="fresh"]')).to_be_visible()
        expect(page.locator("[data-report-focus]").first).to_be_focused()
        expect(page.locator("[data-report-state]")).to_contain_text("0.00")
        with page.expect_response(
            lambda response: response.url == url and response.request.method == "POST"
        ):
            page.locator('button[value="drill"]').click()
        expect(
            page.get_by_role("heading", name=gettext("Authorized no-show sources"))
        ).to_be_visible()
        for width in (1280, 640, 375, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert evaluate_js(
                page, "document.documentElement.scrollWidth <= innerWidth"
            )
            for control in page.locator(
                "#reports-content button, "
                "#reports-content input:not([type=hidden]), "
                "#reports-content select, #reports-content summary"
            ).all():
                if control.is_visible():
                    box = control.bounding_box()
                    assert box is not None
                    assert min(box["height"], box["width"]) >= 44
            report = check_page(
                page, renewal_base_url, renewal_artifact_root, f"reports-{width}"
            )
            assert report["violations"] == 0
            full_page_screenshot(page, root / f"report-{width}.png")
        page.set_viewport_size({"width": 1280, "height": 900})
        page.locator("#id_metric").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#id_start")).to_be_focused()
        page.emulate_media(reduced_motion="reduce", forced_colors="active")
        assert (
            check_page(
                page, renewal_base_url, renewal_artifact_root, "reports-forced-colors"
            )["violations"]
            == 0
        )
        page.emulate_media(forced_colors="none")
        page.locator("#id_question").fill("show all clinics revenue")
        with page.expect_response(
            lambda response: response.url == url and response.request.method == "POST"
        ) as refused:
            page.locator('button[value="ask"]').click()
        assert refused.value.status == 403
        assert "revenue" not in page.url
    with browser.new_context(
        java_script_enabled=False,
        viewport={"width": 375, "height": 900},
        locale="pt-BR",
    ) as native:
        page = native.new_page()
        sign_in_manager(page, renewal_base_url, staff, manager)
        goto_settled(page, url)
        _native_refresh(page)
        full_page_screenshot(page, root / "native-report.png")
