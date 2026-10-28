"""Keyboard capture and physical-window-equivalent 200%/400% reflow evidence."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from renewal.browser._navigation import goto_settled
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import (
    full_page_screenshot,
    grant_capture_audio,
    new_context,
)
from renewal.browser.test_scribe_capture import (
    capture_page,
    fake_microphone,
    open_capture,
    save_manual_note,
    supported,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("capture_page",)


def _activate_with_keyboard(
    page: Page, selector: str, *, backward: bool = False
) -> int:
    """Traverse actual Tab stops in either direction, never script focus."""
    target = page.locator(selector)
    expect(target).to_be_visible()
    expect(target).to_be_enabled()
    assert target.evaluate("el => el.tagName === 'BUTTON'")
    tabs = 0
    for _ in range(120):
        if target.evaluate("el => el === document.activeElement"):
            break
        if backward:
            page.keyboard.press("Shift+Tab")
        else:
            page.keyboard.press("Tab")
        tabs += 1
    expect(target).to_be_focused()
    assert target.evaluate("""el => {
      const style = getComputedStyle(el);
      return style.outlineStyle !== 'none' || style.boxShadow !== 'none';
    }""")
    page.keyboard.press("Space")
    return tabs


@pytest.mark.parametrize("zoom", [2, 4])
def test_keyboard_capture_reflows_at_zoom_with_reduced_motion(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    zoom: int,
) -> None:
    # Given: a 1280x1000 physical window represented at 200% or 400% zoom.
    original = capture_page
    open_capture(
        original,
        renewal_base_url,
        availability_staff,
        "2036-05-02" if zoom == 2 else "2036-05-03",
    )
    browser = original.context.browser
    assert browser is not None
    context = new_context(
        browser,
        media=True,
        viewport={"width": 1280 // zoom, "height": 1000 // zoom},
        device_scale_factor=zoom,
        reduced_motion="reduce",
        locale="pt-BR",
        storage_state=original.context.storage_state(),
    )
    grant_capture_audio(context, renewal_base_url)
    scene = context.new_page()
    scene.set_default_timeout(20_000)
    fake_microphone(scene)
    actions: dict[str, int] = {}
    try:
        goto_settled(scene, original.url)
        scene.evaluate(
            "theme => { document.documentElement.dataset.theme = theme; }",
            "light" if zoom == 2 else "dark",
        )
        panel = scene.locator("[data-scribe-capture]")
        native = supported(scene)

        # When: only Tab, Shift+Tab and Space activate the native controls.
        actions["start"] = _activate_with_keyboard(
            scene, '[data-capture-action="start"]'
        )
        if native:
            expect(panel).to_have_attribute("data-phase", "recording")
            expect(panel).to_have_attribute(
                "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
            )
        else:
            expect(panel).to_have_attribute("data-state", "disabled")
        metrics = scene.evaluate("""() => ({
          css_width: innerWidth, document_width: document.documentElement.scrollWidth,
          device_ratio: devicePixelRatio,
          reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
          buttons: [...document.querySelectorAll('[data-capture-action]')]
            .filter(el => el.getClientRects().length)
            .map(el => ({height: el.getBoundingClientRect().height,
              width: el.getBoundingClientRect().width,
              transition: getComputedStyle(el).transitionDuration}))
        })""")
        full_page_screenshot(
            scene, renewal_artifact_root / f"scribe-zoom-{zoom * 100}.png"
        )
        check_page(
            scene,
            base_url=renewal_base_url,
            artifact_root=renewal_artifact_root,
            state=f"scribe-zoom-{zoom * 100}",
        )
        if native:
            actions["patient_dialog"] = _activate_with_keyboard(
                scene, ".patient-banner-close", backward=True
            )
            expect(scene.locator("[data-capture-dialog]")).to_be_visible()
            scene.screenshot(
                path=str(renewal_artifact_root / f"scribe-dialog-{zoom * 100}.png")
            )
            actions["keep"] = _activate_with_keyboard(
                scene, '[data-capture-choice="keep"]'
            )
            expect(scene.locator("[data-capture-dialog]")).not_to_be_visible()
            expect(panel).to_have_attribute("data-phase", "recording")
            for action, phase in (
                ("pause", "paused"),
                ("resume", "recording"),
                ("stop", "done"),
            ):
                actions[action] = _activate_with_keyboard(
                    scene, f'[data-capture-action="{action}"]'
                )
                expect(panel).to_have_attribute("data-phase", phase)
        else:
            save_manual_note(scene)

        # Then: controls remain operable, visible, bounded, and motion-reduced.
        assert metrics["device_ratio"] == zoom
        assert metrics["css_width"] == 1280 // zoom
        assert metrics["document_width"] <= metrics["css_width"]
        assert metrics["reduced_motion"] is True
        assert metrics["buttons"]
        assert all(
            button["height"] >= 44
            and button["width"] >= 44
            and button["transition"] == "0s"
            for button in metrics["buttons"]
        )
        (renewal_artifact_root / f"accessibility-{zoom * 100}.json").write_text(
            json.dumps(
                {
                    "level": "L1",
                    "zoom_percent": zoom * 100,
                    "method": "device-scale factor and halved/quartered viewport",
                    "native_capture": native,
                    "metrics": metrics,
                    "keyboard_tab_counts": actions,
                    "physical_assistive_technology": "BLOCKED-ON-EG",
                },
                sort_keys=True,
            )
            + "\n"
        )
    finally:
        context.close()
