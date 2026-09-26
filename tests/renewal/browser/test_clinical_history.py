"""Real-surface synthetic history: record, correct, resolve, stale and denied reads."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from renewal.browser.engines import browser_zoom_200, element_box, zoom_screenshot
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_encounter import DAYS, press, seed

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "clinical-history"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def edit(page: Page, kind: str, action: str) -> None:
    with page.expect_navigation():
        page.locator(f'[data-kind="{kind}"] button[value="{action}"]').first.click()


def save(page: Page, expected_status: int = 302) -> None:
    with page.expect_response(lambda r: r.request.method == "POST") as response:
        press(page, "save")
    assert response.value.status == expected_status


def record_and_revise(page: Page, root: Path, kind: str, width: int) -> None:
    section = page.locator(f'[data-kind="{kind}"]')
    expect(section).to_have_attribute("data-state", "not_assessed")
    edit(page, kind, "new")
    page.locator("#id_description").fill(" ")
    page.locator("#id_reason").fill("Avaliação sintética")
    save(page, 400)
    expect(page.locator('[data-save-state="unsaved"]')).to_be_visible()
    expect(section).to_have_attribute("data-state", "not_assessed")
    capture(page, root, f"{kind}-blank-rejected", width)
    page.locator("#id_state").select_option("none_documented")
    page.locator("#id_status").select_option("")
    page.locator("#id_description").fill("")
    save(page)
    expect(section).to_have_attribute("data-state", "none_documented")
    capture(page, root, f"{kind}-explicit-none", width)
    edit(page, kind, "new")
    page.locator("#id_description").fill(f"{kind} sintético original")
    page.locator("#id_reason").fill("Registro clínico sintético")
    save(page)
    expect(section).to_have_attribute("data-revision", "2")
    capture(page, root, f"{kind}-recorded", width)
    edit(page, kind, "edit")
    stale = page.context.new_page()
    # A second editor submits its own bound revision, not mutable session selection.
    stale.goto(page.url)
    edit(stale, kind, "edit")
    page.locator("#id_description").fill(f"{kind} sintético corrigido")
    page.locator("#id_reason").fill("Correção sintética")
    save(page)
    stale.locator("#id_description").fill("Edição antiga não salva")
    stale.locator("#id_reason").fill("Conflito sintético")
    save(stale, 409)
    expect(stale.locator("#id_description")).to_have_value("Edição antiga não salva")
    expect(stale.locator(f'[data-kind="{kind}"]')).to_have_attribute(
        "data-revision", "3"
    )
    capture(stale, root, f"{kind}-stale-rejected", width)
    stale.close()
    edit(page, kind, "edit")
    page.locator("#id_status").select_option("resolved")
    page.locator("#id_reason").fill("Resolução sintética")
    save(page)
    page.reload()
    expect(section.locator("[data-entry]")).to_have_attribute("data-status", "resolved")
    expect(section).to_have_attribute("data-state", "documented")
    section.locator("summary").click()
    expect(section.locator("[data-history-version]")).to_have_count(3)
    expect(section.locator('[data-history-version="1"] h3')).to_have_text(
        f"{kind} sintético original"
    )
    expect(section.locator('[data-history-version="2"] h3')).to_have_text(
        f"{kind} sintético corrigido"
    )
    capture(page, root, f"{kind}-resolved-history", width)


def denied(
    page: Page, staff: dict[str, str], base: str, root: Path, width: int
) -> None:
    # Same-clinic receptionist and cross-clinic request each traverse real middleware.
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    try:
        other = context.new_page()
        _sign_in_receptionist(other, base, staff)
        response = other.goto(page.url)
        assert response is not None
        assert response.status == 403
        assert "sintético corrigido" not in other.content()
        capture(other, root, "reception-denied", width)
        token = page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
        encounter = page.locator('input[name="encounter_id"]').first.input_value()
        post_response = page.request.post(
            f"{base}/ehr/clinics/{staff['clinic_b']}/history/",
            form={
                "csrfmiddlewaretoken": token,
                "action": "open",
                "encounter_id": encounter,
            },
        )
        assert post_response.status == 403
        assert "sintético corrigido" not in post_response.text()
        # Render the actual denied navigation, not a mocked response.
        response_page = page.goto(f"{base}/ehr/clinics/{staff['clinic_b']}/history/")
        assert response_page is not None
        assert response_page.status == 403
        capture(page, root, "other-clinic-denied", width)
    finally:
        context.close()


def reflow(page: Page, root: Path) -> None:
    page.set_viewport_size({"width": 320, "height": 900})
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    edit(page, "problem", "new")
    page.locator("#id_description").fill("L" * 1000)
    page.locator("#id_reason").fill("Conteúdo longo sintético")
    page.locator("#id_description").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#id_status")).to_be_focused()
    capture(page, root, "keyboard-forced-colors-long", 320)
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        java_script_enabled=False,
        storage_state=page.context.storage_state(),
        viewport={"width": 640, "height": 450},
    )
    try:
        native = context.new_page()
        native.goto(page.url)
        edit(native, "problem", "new")
        native.locator("#id_description").fill("L" * 1000)
        native.locator("#id_reason").fill("Registro sem JavaScript")
        save(native)
        expect(native.locator('[data-kind="problem"] [data-entry]')).to_have_count(2)
        capture(native, root, "native-long-no-javascript", 640)
        bounds = element_box(native.locator('button[value="new"]').first)
        assert bounds["height"] >= 44
    finally:
        context.close()


def accessibility_checks(page: Page) -> dict[str, object]:
    expect(page.locator("html")).to_have_attribute("lang", "pt-br")
    expect(page.get_by_role("main")).to_have_count(1)
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    for kind in ("problem", "allergy"):
        expect(page.locator(f'[data-kind="{kind}"]')).to_have_accessible_name(
            re.compile(r"\S")
        )
    fields = page.locator(
        "#history-form textarea, #history-form select, "
        "#history-form input:not([type=hidden])"
    )
    assert fields.count() == 4
    for field in fields.all():
        expect(field).to_have_accessible_name(re.compile(r"\S"))
    buttons = page.locator("main button")
    for button in buttons.all():
        expect(button).to_have_accessible_name(re.compile(r"\S"))
        bounds = element_box(button)
        assert bounds["height"] >= 44
        assert bounds["width"] >= 44
    page.locator("#id_description").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#id_status")).to_be_focused()
    focus = page.locator("#id_status").evaluate("""element => {
        const style = getComputedStyle(element);
        return {outline: style.outlineStyle, width: parseFloat(style.outlineWidth)};
    }""")
    assert focus["outline"] != "none"
    assert focus["width"] >= 2
    page.keyboard.press("Tab")
    expect(page.locator("#id_reason")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator('button[value="save"]')).to_be_focused()
    expect(page.locator('[data-save-state="editing"]')).to_have_attribute(
        "role", "status"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    return {
        "language": "pt-br",
        "main_landmarks": 1,
        "level_one_headings": 1,
        "named_category_regions": 2,
        "labelled_fields": fields.count(),
        "named_buttons_with_44px_targets": buttons.count(),
        "keyboard_order": ["description", "status", "reason", "save"],
        "focus_indicator": focus,
        "editing_status_semantics": True,
        "horizontal_overflow": False,
    }


def zoom_metrics(page: Page) -> dict[str, float]:
    metrics: dict[str, float] = page.evaluate("""() => ({
        inner_width: innerWidth, inner_height: innerHeight,
        device_pixel_ratio: devicePixelRatio, pinch_scale: visualViewport.scale
    })""")
    return metrics


def capture_zoom(page: Page, root: Path, state: str) -> None:
    zoom_screenshot(page, root / "clinical-history" / f"{state}-1280.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def zoom_journey(page: Page, root: Path) -> None:
    """Edit and review history at 200% browser zoom (see engines.browser_zoom_200)."""
    baseline = zoom_metrics(page)
    with browser_zoom_200(page, root) as (zoomed, zoom_method):
        errors: list[str] = []
        console: list[dict[str, str]] = []
        checks: dict[str, object] = {}
        zoomed.on("pageerror", lambda error: errors.append(str(error)))
        zoomed.on(
            "console",
            lambda message: (
                console.append({"type": message.type, "text": message.text})
                if message.type in ("warning", "error")
                else None
            ),
        )
        zoomed.goto(page.url)
        metrics = zoom_metrics(zoomed)
        assert baseline["inner_width"] == 1280
        assert baseline["device_pixel_ratio"] == 1
        assert metrics == {
            "inner_width": 640,
            "inner_height": 450,
            "device_pixel_ratio": 2,
            "pinch_scale": 1,
        }
        for kind in ("problem", "allergy"):
            edit(zoomed, kind, "edit")
            zoomed.locator("#id_description").fill(f"{kind} corrigido em zoom 200%")
            zoomed.locator("#id_reason").fill("Correção com zoom do navegador")
            checks[kind] = accessibility_checks(zoomed)
            capture_zoom(zoomed, root, f"zoom-200-{kind}-editor")
            with zoomed.expect_navigation():
                zoomed.keyboard.press("Enter")
            expect(zoomed.locator('[data-save-state="saved"]')).to_be_visible()
            zoomed.reload()
            section = zoomed.locator(f'[data-kind="{kind}"]')
            expect(section.locator("[data-entry] h3")).to_have_text(
                f"{kind} corrigido em zoom 200%"
            )
            summary = section.locator("summary")
            summary.focus()
            zoomed.keyboard.press("Enter")
            expect(section.locator("details")).to_have_attribute("open", "")
            expect(section.locator("[data-history-version]")).to_have_count(4)
            capture_zoom(zoomed, root, f"zoom-200-{kind}-saved-history")
            assert zoom_metrics(zoomed) == metrics
        (root / "clinical-history" / "accessibility-console-zoom-200.json").write_text(
            json.dumps(
                {
                    "zoom_method": zoom_method,
                    "browser_zoom": metrics["device_pixel_ratio"],
                    "baseline": baseline,
                    "zoomed": metrics,
                    "checks": checks,
                    "native_keyboard_save_and_history_expansion": True,
                    "page_errors": errors,
                    "console_warnings_and_errors": console,
                    "scope": (
                        "History editors and saved versions; not a full WCAG audit"
                    ),
                },
                indent=2,
            )
            + "\n"
        )
        assert not errors
        assert not console


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_clinical_history_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    seed(staff, DAYS[width])
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _sign_in_physician(page, base, staff)
        page.goto(f"{base}/ehr/clinics/{staff['clinic_a']}/history/")
        capture(page, root, "empty", width)
        page.goto(
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{DAYS[width]}/1/"
        )
        press(page, "open")
        with page.expect_navigation():
            page.get_by_role("button", name="Problemas e alergias").click()
        capture(page, root, "not-assessed", width)
        for kind in ("problem", "allergy"):
            record_and_revise(page, root, kind, width)
        if width == 1280:
            zoom_journey(page, root)
        if width == 375:
            reflow(page, root)
        denied(page, staff, base, root, width)
        assert not errors
        (root / "clinical-history" / f"report-{width}.json").write_text(
            json.dumps(
                {
                    "width": width,
                    "page_errors": errors,
                    "stale_status": 409,
                    "blank_status": 400,
                    "denied_status": 403,
                    "retained_versions_per_kind": 3,
                    "reflow": True,
                    "runtime": "clinic_app",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()
