"""Evaluation-specific keyboard, error focus, reflow and motion observations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.ai.evaluation.protocol import ERROR_LABELS
from playwright.sync_api import expect

from renewal.browser._page_wait import evaluate_js
from renewal.browser.engines import full_page_screenshot

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page


def tab_to(page: Page, selector: str) -> None:
    """Reach the next actual control by Tab, never by pointer or DOM focus."""
    sequence = []
    for _ in range(80):
        sequence.append(
            evaluate_js(
                page,
                """() => ({tag: document.activeElement.tagName,
                  name: document.activeElement.getAttribute('name'),
                  id: document.activeElement.id, focused: document.hasFocus()})""",
            )
        )
        if evaluate_js(
            page, "(selector) => document.activeElement.matches(selector)", selector
        ):
            return
        page.keyboard.press("Tab")
    message = f"Evaluation keyboard order did not reach {selector}"
    raise AssertionError(message, sequence)


def fill_rating(page: Page, *, negation: int) -> list[str]:
    """Choose every answer and correction time using actual keyboard input."""
    steps = []
    for label in ERROR_LABELS:
        tab_to(page, f'[data-evaluation-rating] input[name="{label}"]')
        page.keyboard.press("Space")
        value = negation if label == "negation" else 0
        selected = page.locator(
            f'[data-evaluation-rating] input[name="{label}"][value="{value}"]'
        )
        if not selected.is_checked():
            if value == 0:
                page.keyboard.press("ArrowLeft")
            else:
                page.keyboard.press("ArrowRight")
        assert selected.is_checked(), {
            "label": label,
            "expected": value,
            "focus": evaluate_js(
                page,
                """() => ({tag: document.activeElement.tagName,
                  name: document.activeElement.getAttribute('name'),
                  value: document.activeElement.getAttribute('value')})""",
            ),
        }
        steps.append(label)
    tab_to(page, "#id_correction_ms")
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type("1500")
    tab_to(page, '[data-evaluation-rating] button[type="submit"]')
    return [*steps, "correction_ms", "submit"]


def required_states(page: Page, folder: Path, scene: str) -> dict[str, object]:
    """Observe the real required-error page, not the older settings surface."""
    selector = "[data-focus-error]"
    expect(page.locator(selector)).to_be_visible()
    automatic = evaluate_js(
        page, "(selector) => document.activeElement.matches(selector)", selector
    )
    page.locator(selector).focus()
    assert evaluate_js(
        page, "(selector) => document.activeElement.matches(selector)", selector
    )
    page.keyboard.press("Tab")
    first = page.locator('[data-evaluation-rating] input[type="radio"]').first
    assert evaluate_js(first, "(element) => element === document.activeElement")
    original = page.viewport_size
    assert original is not None
    reflow = []
    for factor in (2, 4):
        width, height = 1280 // factor, 900 // factor
        page.set_viewport_size({"width": width, "height": height})
        geometry = evaluate_js(
            page,
            """() => ({
              viewport: innerWidth,
              document: document.documentElement.scrollWidth,
              narrowLabels: [...document.querySelectorAll('.evaluation-choice label')]
                .filter(label => label.getBoundingClientRect().width < 64).length,
              clippedControls: [...document.querySelectorAll(
                '[data-evaluation-rating] input:not([type=hidden]),'
                + '[data-evaluation-rating] button')].filter(element => {
                  const box = element.getBoundingClientRect();
                  return box.left < 0 || box.right > innerWidth;
                }).length,
              overflowing: [...document.querySelectorAll('body *')].flatMap(element => {
                const box = element.getBoundingClientRect();
                if (box.right <= innerWidth || !box.width) return [];
                const style = getComputedStyle(element);
                return [{tag: element.tagName, id: element.id, class: element.className,
                  left: box.left, right: box.right, width: box.width,
                  scroll: element.scrollWidth, client: element.clientWidth,
                  minWidth: style.minWidth, whiteSpace: style.whiteSpace,
                  overflowWrap: style.overflowWrap}];
              }).slice(0, 20)
            })""",
        )
        if geometry["document"] > geometry["viewport"]:
            (folder / f"{scene}-overflow.json").write_text(
                json.dumps(geometry, sort_keys=True, indent=2) + "\n"
            )
        assert geometry["document"] <= geometry["viewport"], geometry
        assert geometry["narrowLabels"] == geometry["clippedControls"] == 0, geometry
        full_page_screenshot(page, folder / f"{scene}-reflow-{factor * 100}.png")
        reflow.append(
            {"percent": factor * 100, "css_viewport": [width, height], **geometry}
        )
    page.set_viewport_size(original)
    motion = """() => ({
      reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
      moving: [...document.querySelectorAll('#evaluation-workspace *')].filter(el => {
        const style = getComputedStyle(el);
        return [style.transitionDuration, style.animationDuration].some(
          durations => durations.split(',').some(value => parseFloat(value) > 0));
      }).length
    })"""
    before = evaluate_js(page, motion)
    assert before["reduce"] is False, before
    assert before["moving"] > 0, before
    page.emulate_media(reduced_motion="reduce")
    reduced = evaluate_js(page, motion)
    assert reduced == {"reduce": True, "moving": 0}, reduced
    page.keyboard.press("Tab")
    assert evaluate_js(page, motion) == reduced
    page.keyboard.press("Shift+Tab")
    assert evaluate_js(
        page,
        "(name) => document.activeElement.getAttribute('name') === name",
        first.get_attribute("name"),
    )
    page.emulate_media(reduced_motion="no-preference")
    return {
        "automatic_error_focus": automatic,
        "summary_focus_and_tab_return_to_first_rating": True,
        "reflow_method": "1280x900 CSS viewport proxies; not browser zoom",
        "reflow": reflow,
        "motion_control": before,
        "reduced_motion": reduced,
    }
