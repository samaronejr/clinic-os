"""Keyboard, announcements and equivalent zoom on the real calculator surface."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import goto_settled
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot, new_context
from renewal.browser.specialty_packs_scenario import submit_pack

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page


def _toggle_calculator_disclosure(calculator: Locator) -> None:
    """Enter on the native summary changes its details state, not the document."""
    summary = calculator.locator("summary")
    summary.focus()
    summary.press("Enter")


def _keyboard_values(page: Page, calculator: Locator, weight: str) -> None:
    for field, value in (
        ("weight", weight),
        ("weight_unit", None),
        ("height", "175"),
        ("height_unit", None),
    ):
        page.keyboard.press("Tab")
        control = calculator.locator(f'[name="bmi-{field}"]')
        expect(control).to_be_focused()
        if value is None:
            page.keyboard.press("Home")
            page.keyboard.press("ArrowDown")
        else:
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.insert_text(value)
    page.keyboard.press("Tab")
    expect(calculator.locator('button[value="calculate"]')).to_be_focused()


def _contrast(page: Page) -> dict[str, object]:
    """Retain measured contrast colors and ratios, not just an empty axe list."""
    measured: dict[str, object] = page.evaluate(
        """async () => {
          const result = await axe.run(
            document.querySelector('[data-calculator="bmi"]'),
            {runOnly: {type: 'rule', values: ['color-contrast']},
             resultTypes: ['passes', 'violations', 'incomplete']});
          return {
            violations: result.violations.map(rule => rule.id),
            incomplete: result.incomplete.map(rule => rule.id),
            passed: result.passes.flatMap(rule => rule.nodes.map(node => ({
              target: node.target,
              measurements: [...node.any, ...node.all].map(check => check.data),
            }))),
          };
        }"""
    )
    assert measured["violations"] == []
    assert measured["incomplete"] == []
    assert measured["passed"]
    return measured


def test_specialty_pack_keyboard_announces_refusal_and_result(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    filled_bmi: Locator,
    browser_report: dict[str, object],
) -> None:
    page = renewal_page
    calculator = filled_bmi
    _toggle_calculator_disclosure(calculator)
    expect(calculator).not_to_have_attribute("open", "")
    _toggle_calculator_disclosure(calculator)
    expect(calculator).to_have_attribute("open", "")
    for key, field in (("weight", "Weight"), ("height", "Height")):
        unit = "kg or g" if key == "weight" else "cm or m"
        label = f"{gettext(field)} ({gettext(unit)})"
        expect(calculator.get_by_label(label)).to_have_attribute("name", f"bmi-{key}")
    _keyboard_values(page, calculator, "1,234.5")
    submit_pack(
        page,
        calculator.locator('button[value="calculate"]'),
        status=400,
        keyboard=True,
    )
    error = calculator.locator("[data-focus-error]")
    expect(error).to_be_focused()
    expect(error).to_have_attribute("role", "alert")
    expect(error).to_contain_text(
        gettext("Enter valid values with a decimal point and explicit units.")
    )
    expect(calculator.locator("[data-calculation-result]")).to_have_count(0)
    folder = renewal_artifact_root / "specialty-packs"
    folder.mkdir(mode=0o700, exist_ok=True)
    full_page_screenshot(page, folder / "keyboard-error-focus.png")
    check_page(
        page, renewal_base_url, renewal_artifact_root, "calculator-keyboard-error"
    )
    refusal_contrast = _contrast(page)
    _keyboard_values(page, calculator, "70")
    submit_pack(
        page,
        calculator.locator('button[value="calculate"]'),
        keyboard=True,
    )
    result = calculator.locator("[data-calculation-result]")
    expect(result).to_contain_text("22.86")
    expect(result).to_have_attribute("role", "status")
    expect(result).to_be_focused()
    full_page_screenshot(page, folder / "keyboard-result-focus.png")
    check_page(
        page, renewal_base_url, renewal_artifact_root, "calculator-keyboard-result"
    )
    browser_report["contrast"] = {
        "refusal": refusal_contrast,
        "result": _contrast(page),
    }
    browser_report["checks"] = [
        "keyboard-only-disclosure-form-and-submit",
        "catalog-labels-associated-with-inputs",
        "refusal-alert-focused-no-result",
        "result-status-focused",
    ]


@pytest.mark.parametrize("scale", [2, 4])
def test_specialty_pack_zoom_and_reduced_motion(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    filled_bmi: Locator,
    browser_report: dict[str, object],
    scale: int,
) -> None:
    """1280x900 window at 200/400% equivalent reflow; no CSS zoom override."""
    page = filled_bmi.page
    expect(filled_bmi).to_be_visible()
    browser = page.context.browser
    assert browser
    reports = []
    for theme in ("light", "dark"):
        with new_context(
            browser,
            locale="pt-BR",
            storage_state=page.context.storage_state(),
            viewport={"width": 1280 // scale, "height": 900 // scale},
            device_scale_factor=scale,
            reduced_motion="reduce",
        ) as context:
            zoomed = context.new_page()
            goto_settled(zoomed, page.url)
            zoomed.evaluate(
                "theme => document.documentElement.dataset.theme = theme", theme
            )
            calculator = zoomed.locator('[data-calculator="bmi"]')
            _toggle_calculator_disclosure(calculator)
            expect(calculator).to_have_attribute("open", "")
            _keyboard_values(zoomed, calculator, "70")
            submit_pack(
                zoomed,
                calculator.locator('button[value="calculate"]'),
                keyboard=True,
            )
            result = calculator.locator("[data-calculation-result]")
            expect(result).to_contain_text("22.86")
            expect(result).to_be_focused()
            metrics = zoomed.evaluate(
                """() => ({
                  width: innerWidth,
                  dpr: devicePixelRatio,
                  reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
                  overflow: document.documentElement.scrollWidth > innerWidth,
                })"""
            )
            assert metrics == {
                "width": 1280 // scale,
                "dpr": scale,
                "reduced": True,
                "overflow": False,
            }
            motion = calculator.locator("input, select, button, summary").evaluate_all(
                """elements => elements.map(node => getComputedStyle(node))
                  .filter(style => style.transitionDuration.split(',')
                    .some(value => parseFloat(value) > 0)
                    || style.animationDuration.split(',')
                    .some(value => parseFloat(value) > 0)).length"""
            )
            assert motion == 0
            folder = renewal_artifact_root / "specialty-packs"
            folder.mkdir(mode=0o700, exist_ok=True)
            name = f"calculator-zoom-{scale * 100}-{theme}-reduced"
            full_page_screenshot(zoomed, folder / f"{name}.png")
            zoomed.screenshot(path=str(folder / f"{name}-focus.png"))
            reports.append(
                {
                    "axe": check_page(
                        zoomed, renewal_base_url, renewal_artifact_root, name
                    ),
                    "contrast": _contrast(zoomed),
                }
            )
    browser_report["checks"] = [
        "equivalent-zoom-reflow",
        "keyboard-calculation-at-zoom",
        "reduced-motion-no-transitions-or-animations",
        "light-dark-contrast",
    ]
    browser_report[f"calculator_zoom_{scale}"] = {"scale": scale, "axe": reports}
