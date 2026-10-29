"""Native settings forms, real clinic_app serving, scoped branding and denial."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import psycopg
import pytest
from django.utils.translation import gettext
from PIL import Image
from playwright.sync_api import expect

from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
    reload_settled,
)
from renewal.browser._page_wait import evaluate_js, wait_for_js
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import (
    browser_zoom_200,
    focus_reveal,
    focuses_dialogs_and_scrollers,
    full_page_screenshot,
    new_context,
    scroll_width_includes_flex_end_padding,
    selected_engine,
    zoom_200,
    zoom_screenshot,
)
from renewal.browser.evaluation_journeys import blinded_journey
from renewal.browser.support_managers import seed_ai_admin, seed_settings_manager
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_primitives import (
    CYAN,
    FOCUS_JS,
    MIN_RING_PX,
    NAVY,
    OVERFLOW_JS,
    SCROLL_SETTLED_JS,
    TABBABLE_JS,
    _px,
)
from renewal.browser.test_retention import post_action, sign_in_manager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

AI_FORM: Final = "[data-ai-policy-form]"
AI_PRESENTATIONS: Final = (
    (1280, "light"),
    (1280, "dark"),
    (375, "light"),
    (375, "dark"),
)
MAX_TAB_STOPS: Final = 200
# A 1280x900 window at 400% zoom lays out a 320x225 CSS viewport (WCAG 1.4.10).
ZOOM_400_VIEWPORT: Final = {"width": 320, "height": 225}
ZOOM_400_SCALE: Final = 4
ZOOM_200_METRICS: Final = {"inner_width": 640, "inner_height": 450, "ratio": 2}
ZOOM_400_METRICS: Final = {"inner_width": 320, "inner_height": 225, "ratio": 4}
ZOOM_JS: Final = (
    "() => ({inner_width: innerWidth, inner_height: innerHeight,"
    " ratio: devicePixelRatio})"
)
FOCUSED_IS_JS: Final = (
    "(selector) => document.activeElement === document.querySelector(selector)"
)
IN_MAIN_JS: Final = "() => Boolean(document.activeElement?.closest('main'))"
SUMMARY_FOCUSED_JS: Final = "() => document.activeElement.matches('[data-focus-error]')"
REVEAL_JS: Final = (
    "() => document.activeElement.scrollIntoView({block: 'nearest', inline: 'nearest'})"
)
FORM_CONTROL_INDEXES_JS: Final = """(selector) => {
  const all = Array.from(document.querySelectorAll('*'));
  return Array.from(document.querySelector(selector).querySelectorAll(
    'input:not([type=hidden]), select, textarea, button'))
    .map((el) => all.indexOf(el));
}"""
# What a sighted user reads from the layout must be programmatic too: each
# label names a control, each hint or error describes its control, every
# aria-describedby id exists, and a checkbox group carries a group name.
ASSOCIATION_JS: Final = """(form) => {
  const problems = [];
  const controls = form.querySelectorAll('input:not([type=hidden]), select, textarea');
  for (const el of controls) {
    const name = el.id || el.name;
    if (el.labels.length === 0 && !el.getAttribute('aria-labelledby')) {
      problems.push(`${name}: no label`);
    }
    for (const id of (el.getAttribute('aria-describedby') || '').split(/\\s+/)) {
      if (id && !document.getElementById(id)) {
        problems.push(`${name}: aria-describedby "${id}" does not exist`);
      }
    }
    const peers = form.querySelectorAll(`input[type=checkbox][name="${el.name}"]`);
    if (el.type === 'checkbox' && peers.length > 1) {
      const group = el.closest('fieldset, [role=group]');
      const named = group && (group.matches('fieldset')
        ? group.querySelector(':scope > legend')?.textContent.trim()
        : document.getElementById(group.getAttribute('aria-labelledby') || '')
          ?.textContent.trim());
      if (!named) problems.push(`${name}: checkbox group has no group name`);
    }
  }
  for (const label of form.querySelectorAll('label')) {
    if (!label.control) {
      problems.push(`label "${label.textContent.trim()}" names no control`);
    }
  }
  for (const field of form.querySelectorAll('.field')) {
    const described = new Set(Array.from(field.querySelectorAll('[aria-describedby]'))
      .flatMap((el) => el.getAttribute('aria-describedby').split(/\\s+/)));
    for (const text of field.querySelectorAll('p, .errorlist')) {
      if (text.closest('label')) continue;
      let node = text;
      while (node !== field && !(node.id && described.has(node.id))) {
        node = node.parentElement;
      }
      if (node === field) {
        problems.push(`"${text.textContent.trim().slice(0, 60)}" describes no control`);
      }
    }
  }
  return problems;
}"""
OUTSIDE_JS: Final = """(form) => Array.from(form.querySelectorAll(
  'label, input:not([type=hidden]), button, p, li, legend')).filter((el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && (rect.left < -1 || rect.right > innerWidth + 1);
  }).map((el) => el.id || el.tagName + ' ' + el.textContent.trim().slice(0, 40))"""
FIELD_STATE_JS: Final = """(el) => ({
  invalid: el.getAttribute('aria-invalid'),
  describedby: el.getAttribute('aria-describedby'),
  descriptions: (el.getAttribute('aria-describedby') || '').split(/\\s+/)
    .filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent.trim() ?? null),
})"""
# Where each checkbox's own label text sits relative to its box.
CHECKBOX_LAYOUT_JS: Final = """(form) => Array.from(
  form.querySelectorAll('input[type=checkbox]')).map((box) => {
    const edges = (rect) => [rect.left, rect.top, rect.right, rect.bottom]
      .map(Math.round);
    const label = box.labels[0];
    const words = label && Array.from(label.childNodes).find(
      (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    const range = document.createRange();
    if (words) range.selectNodeContents(words);
    return {id: box.id, box: edges(box.getBoundingClientRect()),
            text: words ? edges(range.getBoundingClientRect()) : null};
  })"""
MOTION_JS: Final = """() => {
  const moving = (value) => value.split(',').some((part) => parseFloat(part) > 0);
  let transitions = 0;
  let animations = 0;
  for (const el of document.querySelectorAll('*')) {
    for (const pseudo of [null, '::before', '::after']) {
      const style = getComputedStyle(el, pseudo);
      if (style.transitionProperty !== 'none' && moving(style.transitionDuration)) {
        transitions += 1;
      }
      if (style.animationName !== 'none' && moving(style.animationDuration)) {
        animations += 1;
      }
    }
  }
  return {
    reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
    transitions, animations, running: document.getAnimations().length,
  };
}"""
# The same rule set as a11y_support.check_page, for the results axe-core
# cannot decide alone ("needs review"): kept as evidence, never asserted.
AXE_REVIEW_JS: Final = """async () => (await axe.run(document, {
    runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
      'wcag22aa', 'best-practice']},
    resultTypes: ['incomplete'],
  })).incomplete.map((v) => ({
    id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.slice(0, 8).map((n) => n.target.join(' ')),
    count: v.nodes.length,
  }))"""


def submit(page: Page, action: str) -> None:
    click_to_navigate(page.locator(f'button[value="{action}"]'))


def capture(page: Page, root: Path, scene: str, width: int) -> None:
    destination = root / "clinic-settings"
    destination.mkdir(exist_ok=True)
    full_page_screenshot(page, destination / f"{scene}-{width}.png")
    assert evaluate_js(page, "document.documentElement.scrollWidth <= innerWidth")


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
    goto_settled(page, other)
    other_name = page.locator("#id_display_name").input_value()
    expect(page.locator("[data-configuration-version]")).to_have_text("0")
    goto_settled(page, url)
    version = publish_brand(page, width)
    capture(page, renewal_artifact_root, "published", width)
    reload_settled(page)
    expect(page.locator("[data-configuration-version]")).to_have_text(str(version))
    publish_overlays(page, width)
    capture(page, renewal_artifact_root, "overlays", width)
    rejected_changes(page, url, version)
    capture(page, renewal_artifact_root, "rejected", width)
    goto_settled(page, other)
    expect(page.locator("#id_display_name")).to_have_value(other_name)
    expect(page.locator("[data-configuration-version]")).to_have_text("0")
    expect(page.locator("[data-clinic-brand]")).to_have_count(0)
    capture(page, renewal_artifact_root, "other-clinic-unchanged", width)
    goto_settled(page, url)
    if width == 375:
        page.set_viewport_size({"width": 320, "height": 900})
        capture(page, renewal_artifact_root, "reflow", 320)
        page.emulate_media(forced_colors="active", reduced_motion="reduce")
        capture(page, renewal_artifact_root, "forced-colors", 320)
        page.emulate_media(forced_colors="none")
        zoom_context, zoomed = zoom_200(page)
        goto_settled(zoomed, url)
        assert zoomed.evaluate("[devicePixelRatio, innerWidth]") == [2, 640]
        capture(zoomed, renewal_artifact_root, "zoom-200-layout", 640)
        zoom_context.close()
    context.close()
    physician_context = browser.new_context(viewport={"width": width, "height": 900})
    physician = physician_context.new_page()
    _sign_in_physician(physician, renewal_base_url, staff)
    response = goto_settled(physician, url)
    assert response is not None
    assert response.status == 403
    expect(physician.locator('[data-module="settings"]')).to_have_count(0)
    capture(physician, renewal_artifact_root, "physician-denied", width)
    physician_context.close()


@pytest.mark.parametrize(
    "presentation",
    [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")],
)
def test_ai_organization_controls_are_native_scoped_and_readable(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    presentation: tuple[int, str],
) -> None:
    """Extend this registered suite; no new suite or CI shard is introduced."""
    width, theme = presentation
    staff = availability_staff
    # Owner-only synthetic setup. Runtime assertions below use only the UI.
    manager = seed_ai_admin(staff, theme)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False, viewport={"width": width, "height": 900}
    )
    try:
        page = context.new_page()
        sign_in_manager(page, renewal_base_url, staff, manager)
        settings_url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/"
        goto_settled(page, settings_url)
        page.locator("#id_display_name").fill("Clínica sintética de IA")
        submit(page, "settings")
        ai_url = f"{settings_url}ai/"
        goto_settled(page, ai_url)
        expect(page.locator("h1")).to_have_text(gettext("AI settings"))
        expect(page.locator("[data-ai-status]")).to_contain_text(
            gettext("AI is disabled. Continue working manually.")
        )
        page.locator("#id_monthly_budget_micros").fill("100000")
        page.locator("#id_enabled").check()
        page.locator("#id_limit_front_desk").fill("100000")
        page.locator('input[name="purposes"][value="front_desk"]').check()
        click_to_navigate(page.locator('[data-ai-policy-form] button[type="submit"]'))
        expect(page.locator("#id_monthly_budget_micros")).to_have_value("100000")
        expect(page.locator("#id_enabled")).to_be_checked()
        capture(page, renewal_artifact_root, f"ai-controls-{theme}", width)
        page.locator("#id_monthly_budget_micros").fill("-1")
        click_to_navigate(page.locator('[data-ai-policy-form] button[type="submit"]'))
        expect(page.locator('[data-ai-policy-form] [role="alert"]')).to_be_visible()
        capture(page, renewal_artifact_root, f"ai-invalid-{theme}", width)
    finally:
        context.close()


@pytest.mark.parametrize(
    "presentation",
    [
        (1280, "light", False),
        (375, "dark", False),
        (1280, "light", True),
        (375, "dark", True),
    ],
    ids=["native-desktop", "native-mobile", "hx-desktop", "hx-mobile"],
)
def test_blinded_two_rater_adjudication(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    presentation: tuple[int, str, bool],
) -> None:
    """Extend clinic-settings; the standard runner exercises every browser engine."""
    blinded_journey(
        renewal_page,
        renewal_base_url,
        renewal_artifact_root,
        availability_staff,
        presentation=presentation,
    )


@dataclasses.dataclass
class AIScene:
    """One AI-form accessibility case: what it opens and where evidence goes.

    ``problems`` collects every failed SC-15 check with its geometry; the
    test asserts it empty only after the whole case has been recorded.
    ``shell`` records focus reveal outside ``main`` (the shared header and
    settings tab strip): evidence for their owners, not an AI-form failure.
    """

    base: str
    url: str
    root: Path
    folder: Path
    theme: str
    case: str
    phase: str = "initial"
    problems: list[dict[str, object]] = dataclasses.field(default_factory=list)
    shell: list[dict[str, object]] = dataclasses.field(default_factory=list)

    def flag(self, check: str, detail: dict[str, object]) -> None:
        self.problems.append({"check": check, "phase": self.phase, **detail})


def shoot(
    page: Page, scene: AIScene, name: str, *, viewport: bool = False, zoom: bool = False
) -> list[str]:
    """Capture the page (whole, viewport only, or at real browser zoom)."""
    scene.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = scene.folder / f"{name}.png"
    if zoom:
        zoom_screenshot(page, destination)
    elif viewport:
        page.screenshot(path=str(destination))
    else:
        return [path.name for path in full_page_screenshot(page, destination)]
    destination.chmod(0o600)
    return [destination.name]


def audit(page: Page, scene: AIScene, state: str) -> dict[str, object]:
    """Axe with no serious/critical violation, plus what axe left for review."""
    name = f"ai-{scene.case}-{state}"
    result = check_page(page, scene.base, scene.root, name)
    review = evaluate_js(page, AXE_REVIEW_JS)
    destination = scene.root / "axe" / f"{name}-needs-review.json"
    destination.write_text(json.dumps(review, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)
    return {**result, "needs_review": sorted({item["id"] for item in review})}


def associations(page: Page) -> list[str]:
    problems: list[str] = evaluate_js(page.locator(AI_FORM), ASSOCIATION_JS)
    return problems


def checkbox_layout(page: Page, scene: AIScene) -> list[dict[str, object]]:
    """Each checkbox's label text sits beside its box (DESIGN.md settings-check)."""
    rows: list[dict[str, Any]] = evaluate_js(page.locator(AI_FORM), CHECKBOX_LAYOUT_JS)
    for row in rows:
        box, words = row["box"], row["text"]
        middle = None if words is None else (words[1] + words[3]) / 2
        if middle is None or not box[1] <= middle <= box[3]:
            scene.flag("checkbox label text beside its box", row)
    return rows


def ring_ok(info: dict[str, Any]) -> bool:
    return bool(
        info["focusVisible"]
        and info["outlineStyle"] == "solid"
        and _px(info["outlineWidth"]) >= MIN_RING_PX
    )


def focused(page: Page, scene: AIScene) -> dict[str, Any] | None:
    """The focused element, revealed as the engine reveals it, ring recorded."""
    wait_for_js(page, SCROLL_SETTLED_JS)
    info: dict[str, Any] | None = evaluate_js(page, FOCUS_JS)
    if info is None:
        return None
    reveal = focus_reveal(page.context, text_entry=info["textEntry"])
    if reveal == "none":
        # Firefox can leave the focused control off screen (engines.py): bring
        # it to view as a user would, then require its whole box on screen.
        evaluate_js(page, REVEAL_JS)
        wait_for_js(page, SCROLL_SETTLED_JS)
        info = evaluate_js(page, FOCUS_JS)
        assert info is not None
    info["inMain"] = evaluate_js(page, IN_MAIN_JS)
    keys = ("label", "text", "focusVisible", "outlineStyle", "outlineWidth")
    detail = {key: info[key] for key in (*keys, "outlineColor", "inMain", "rect")}
    detail["viewport"] = info["viewport"]
    if not ring_ok(info):
        scene.flag("visible focus ring", detail)
    # The Two-Ring Rule: navy on paper, cyan on every dark-theme surface.
    if info["inMain"] and info["outlineColor"] != (
        CYAN if scene.theme == "dark" else NAVY
    ):
        scene.flag("brand focus ring colour", detail)
    if not info["visible" if reveal == "visible" else "within"]:
        check = f"focused element revealed ({reveal})"
        if info["inMain"]:
            scene.flag(check, detail)
        else:
            scene.shell.append({"check": check, "phase": scene.phase, **detail})
    return info


def walk_tab_order(page: Page, scene: AIScene, state: str) -> dict[str, object]:
    """Tab through a fresh document: every control, in DOM order."""
    tabbable = set(evaluate_js(page, TABBABLE_JS))
    form: list[int] = evaluate_js(page, FORM_CONTROL_INDEXES_JS, AI_FORM)
    captured = {form[0], form[1], form[len(form) // 2], form[-1]}
    stops: list[dict[str, Any]] = []
    for _ in range(MAX_TAB_STOPS):
        page.keyboard.press("Tab")
        info = focused(page, scene)
        # Focus leaves the document after the last stop, or stays on it in
        # headless Firefox (engines.py, Tab past the end).
        if info is None or (stops and info["domIndex"] <= stops[-1]["domIndex"]):
            break
        stops.append(info)
        if info["domIndex"] in captured:
            shoot(page, scene, f"{state}-focus-{len(stops):02d}", viewport=True)
    else:
        pytest.fail("Tab never left the last control")
    visited = [stop["domIndex"] for stop in stops]
    assert tabbable <= set(visited), sorted(tabbable - set(visited))
    extra = [stop for stop in stops if stop["domIndex"] not in tabbable]
    if focuses_dialogs_and_scrollers(page.context):
        assert all(stop["tag"] == "DIALOG" or stop["scroller"] for stop in extra), extra
    else:
        assert extra == [], extra
    assert [index for index in visited if index in form] == form
    return {
        "tab_stops": len(stops),
        "tabbable": len(tabbable),
        "order": [stop["label"] for stop in stops],
        "form_order": [stop["label"] for stop in stops if stop["domIndex"] in form],
        "ring_colors": sorted({stop["outlineColor"] for stop in stops}),
    }


def tab_to(page: Page, scene: AIScene, selector: str) -> None:
    """Tab forward until ``selector`` holds focus; every stop is recorded."""
    for _ in range(MAX_TAB_STOPS):
        page.keyboard.press("Tab")
        focused(page, scene)
        if evaluate_js(page, FOCUSED_IS_JS, selector):
            return
    pytest.fail(f"{selector} never received keyboard focus")


def toggle_on(page: Page, selector: str) -> list[bool]:
    """Space toggles the focused checkbox; leave it checked."""
    box = page.locator(selector)
    states = [box.is_checked()]
    page.keyboard.press("Space")
    states.append(box.is_checked())
    if not states[-1]:
        page.keyboard.press("Space")
        states.append(box.is_checked())
    assert states[1] is not states[0], states
    assert states[-1], states
    return states


def type_value(page: Page, value: str) -> None:
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(value)


def keyboard_submit(page: Page) -> int:
    with expect_document(page) as navigation:
        page.keyboard.press("Enter")
    response = navigation.value
    assert response is not None
    return response.status


def keyboard_save(page: Page, scene: AIScene) -> dict[str, object]:
    """Keyboard only: opt in, choose a capability, set both limits, save."""
    scene.phase = "keyboard-save"
    goto_settled(page, scene.url)
    purpose = 'input[name="purposes"][value="front_desk"]'
    tab_to(page, scene, "#id_enabled")
    enabled = toggle_on(page, "#id_enabled")
    tab_to(page, scene, purpose)
    chosen = toggle_on(page, purpose)
    tab_to(page, scene, "#id_monthly_budget_micros")
    type_value(page, "250000")
    tab_to(page, scene, "#id_limit_front_desk")
    type_value(page, "120000")
    tab_to(page, scene, f"{AI_FORM} button[type=submit]")
    status = keyboard_submit(page)
    assert status == 200
    assert page.url == scene.url
    expect(page.locator("#id_enabled")).to_be_checked()
    expect(page.locator(purpose)).to_be_checked()
    expect(page.locator("#id_monthly_budget_micros")).to_have_value("250000")
    expect(page.locator("#id_limit_front_desk")).to_have_value("120000")
    return {
        "status": status,
        "enabled_checkbox_states": enabled,
        "capability_checkbox_states": chosen,
        "axe": audit(page, scene, "saved"),
        "screenshots": shoot(page, scene, "saved"),
    }


def keyboard_error(page: Page, scene: AIScene, state: str) -> dict[str, Any]:
    """An invalid limit by keyboard: 400, focus on the error summary, Tab on."""
    scene.phase = f"{state}-keyboard-error"
    tab_to(page, scene, "#id_monthly_budget_micros")
    type_value(page, "-1")
    status = keyboard_submit(page)
    assert status == 400
    assert evaluate_js(page, SUMMARY_FOCUSED_JS)
    summary = evaluate_js(page, FOCUS_JS)
    if not ring_ok(summary) or summary["outlineColor"] != (
        CYAN if scene.theme == "dark" else NAVY
    ):
        scene.flag("error summary focus ring", summary)
    shot = shoot(page, scene, f"{state}-error-summary-focus", viewport=True)
    budget = evaluate_js(page.locator("#id_monthly_budget_micros"), FIELD_STATE_JS)
    assert budget["invalid"] == "true", budget
    page.keyboard.press("Tab")
    after = focused(page, scene)
    assert after is not None
    assert after["label"] == "#id_enabled", after
    return {
        "status": status,
        "summary_ring": summary["outlineColor"],
        "summary_focus_visible": summary["focusVisible"],
        "summary_screenshot": shot,
        "invalid_field": budget,
        "tab_after_summary": after["label"],
    }


def reflow(page: Page, scene: AIScene) -> dict[str, Any]:
    """No sideways page scroll, no box wider than its container, all in view."""
    flex = str(scroll_width_includes_flex_end_padding(page.context)).lower()
    result = {
        "scroll_width": evaluate_js(page, "document.documentElement.scrollWidth"),
        "inner_width": evaluate_js(page, "innerWidth"),
        "overflowing": evaluate_js(
            page.locator("main"), f"(root) => ({OVERFLOW_JS})(root, {flex})"
        ),
        "outside_viewport": evaluate_js(page.locator(AI_FORM), OUTSIDE_JS),
    }
    if result["scroll_width"] > result["inner_width"]:
        scene.flag("no sideways page scroll", result)
    if result["overflowing"]:
        scene.flag("no box wider than its container", result)
    if result["outside_viewport"]:
        scene.flag("every form element inside the viewport", result)
    result["checkboxes"] = checkbox_layout(page, scene)
    return result


def reduced_motion(page: Page, scene: AIScene) -> dict[str, object]:
    """Reduced motion leaves no transition or animation, even on focus moves."""
    scene.phase = "reduced-motion"
    before = evaluate_js(page, MOTION_JS)
    page.emulate_media(reduced_motion="reduce")
    reduced = evaluate_js(page, MOTION_JS)
    page.keyboard.press("Tab")
    focused(page, scene)
    after_focus = evaluate_js(page, MOTION_JS)
    # The control: without the preference the page does have transitions.
    assert before["reduce"] is False, before
    assert before["transitions"] > 0, before
    still = {"reduce": True, "transitions": 0, "animations": 0, "running": 0}
    assert reduced == still, reduced
    assert after_focus == still, after_focus
    return {
        "no_preference": before,
        "reduce": reduced,
        "reduce_after_focus_move": after_focus,
        "screenshots": shoot(page, scene, "reduced-motion"),
    }


@contextmanager
def zoom_200_page(page: Page, root: Path) -> Iterator[tuple[Page, str]]:
    """Real 200% zoom on Chromium and Firefox; WebKit uses the scale proxy.

    The WebKit proxy blocks service workers: there a fetch-handling worker
    hides a controlled page's requests (engines.py, Request interception),
    including the axe-core response check_page asserts.
    """
    if selected_engine() != "webkit":
        with browser_zoom_200(page, root) as zoomed:
            yield zoomed
        return
    browser = page.context.browser
    assert browser is not None
    context = new_context(
        browser,
        locale="pt-BR",
        viewport={"width": 640, "height": 450},
        device_scale_factor=2,
        service_workers="block",
        storage_state=page.context.storage_state(),
    )
    try:
        yield context.new_page(), "device_scale_factor=2 at 640x450 (Playwright #2497)"
    finally:
        context.close()


@contextmanager
def zoom_400_page(page: Page) -> Iterator[Page]:
    """A 1280x900 window at 400%: 320x225 CSS px at device scale factor 4.

    Playwright has no zoom API (#2497); engines.new_context keeps the scale on
    Firefox through layout.css.devPixelsPerPx across every navigation.
    """
    browser = page.context.browser
    assert browser is not None
    context = new_context(
        browser,
        locale="pt-BR",
        viewport=ZOOM_400_VIEWPORT,
        device_scale_factor=ZOOM_400_SCALE,
        service_workers="block",
        storage_state=page.context.storage_state(),
    )
    try:
        yield context.new_page()
    finally:
        context.close()


def zoom_journey(
    zoomed: Page, scene: AIScene, level: str, expected: dict[str, int], *, zoom: bool
) -> dict[str, object]:
    """Valid and invalid form at one zoom level: reflow, axe, keyboard."""
    scene.phase = f"zoom-{level}"
    goto_settled(zoomed, scene.url)
    assert evaluate_js(zoomed, ZOOM_JS) == expected
    valid = reflow(zoomed, scene)
    shots = shoot(zoomed, scene, f"zoom-{level}-valid", zoom=zoom)
    axe_valid = audit(zoomed, scene, f"zoom-{level}-valid")
    keyboard = walk_tab_order(zoomed, scene, f"zoom-{level}")
    goto_settled(zoomed, scene.url)
    error = keyboard_error(zoomed, scene, f"zoom-{level}")
    # Still zoomed after the form POST (a COOP context swap keeps the scale).
    assert evaluate_js(zoomed, ZOOM_JS) == expected
    invalid = reflow(zoomed, scene)
    shots += shoot(zoomed, scene, f"zoom-{level}-invalid", zoom=zoom)
    return {
        "metrics": expected,
        "valid_reflow": valid,
        "invalid_reflow": invalid,
        "axe": [axe_valid, audit(zoomed, scene, f"zoom-{level}-invalid")],
        "keyboard": keyboard,
        "error": error,
        "associations": associations(zoomed),
        "screenshots": shots,
    }


def write_evidence(scene: AIScene, evidence: dict[str, Any]) -> None:
    scene.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = scene.folder / "evidence.json"
    evidence["problems"] = scene.problems
    evidence["shell_focus_reveal"] = scene.shell
    destination.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)


@pytest.mark.parametrize(
    "presentation", AI_PRESENTATIONS, ids=[f"{w}-{t}" for w, t in AI_PRESENTATIONS]
)
def test_ai_settings_form_meets_the_accessibility_matrix(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    presentation: tuple[int, str],
) -> None:
    """SC-15 on the AI form: axe, keyboard script, 200%/400% reflow, reduced motion.

    Ring, reveal, reflow, checkbox layout and label/hint/group association
    failures are recorded with their geometry and asserted last, once the
    case's evidence file is written.
    """
    width, theme = presentation
    staff = availability_staff
    manager = seed_ai_admin(staff, theme)
    browser = renewal_page.context.browser
    assert browser is not None
    settings_url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/"
    case = f"{theme}-{width}"
    scene = AIScene(
        base=renewal_base_url,
        url=f"{settings_url}ai/",
        root=renewal_artifact_root,
        folder=renewal_artifact_root / "clinic-settings" / "ai-a11y" / case,
        theme=theme,
        case=case,
    )
    evidence: dict[str, Any] = {"engine": selected_engine(), "case": case}
    context = browser.new_context(
        locale="pt-BR",
        viewport={"width": width, "height": 900},
        service_workers="block",
    )
    try:
        page = context.new_page()
        sign_in_manager(page, renewal_base_url, staff, manager)
        goto_settled(page, settings_url)
        page.locator("#id_display_name").fill("Clínica sintética acessível")
        submit(page, "settings")
        goto_settled(page, scene.url)
        assert evaluate_js(page, "document.documentElement.dataset.theme") == theme
        evidence["initial"] = {
            "axe": audit(page, scene, "initial"),
            "screenshots": shoot(page, scene, "initial"),
            "associations": associations(page),
            "reflow": reflow(page, scene),
            "keyboard": walk_tab_order(page, scene, "initial"),
        }
        evidence["saved"] = keyboard_save(page, scene)
        evidence["invalid"] = keyboard_error(page, scene, "invalid")
        evidence["invalid"].update(
            axe=audit(page, scene, "invalid"),
            screenshots=shoot(page, scene, "invalid"),
            associations=associations(page),
        )
        evidence["reduced_motion"] = reduced_motion(page, scene)
        if width == 1280:
            with zoom_200_page(page, renewal_artifact_root) as (zoomed, method):
                journey = zoom_journey(
                    zoomed, scene, "200", ZOOM_200_METRICS, zoom=True
                )
                evidence["zoom_200"] = {"method": method, **journey}
            with zoom_400_page(page) as zoomed:
                journey = zoom_journey(
                    zoomed, scene, "400", ZOOM_400_METRICS, zoom=False
                )
                evidence["zoom_400"] = {"method": "device_scale_factor=4", **journey}
    finally:
        write_evidence(scene, evidence)
        context.close()
    assert evidence["initial"]["associations"] == []
    assert evidence["invalid"]["associations"] == []
    assert scene.problems == []


@pytest.mark.parametrize(
    "presentation",
    [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")],
)
def test_agent_queue_and_policy_controls(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    presentation: tuple[int, str],
) -> None:
    width, theme = presentation
    staff = availability_staff
    manager = seed_ai_admin(staff, theme)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(viewport={"width": width, "height": 900})
    try:
        page = context.new_page()
        sign_in_manager(page, renewal_base_url, staff, manager)
        queue = f"{renewal_base_url}/clinics/{staff['clinic_a']}/automations/agents/"
        policy = f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/agents/"
        goto_settled(page, queue)
        expect(page.locator("[data-agent-empty]")).to_be_visible()
        capture(page, renewal_artifact_root, f"agents-empty-{theme}", width)
        check_page(
            page,
            renewal_base_url,
            renewal_artifact_root,
            f"agents-empty-{theme}-{width}",
        )
        goto_settled(page, policy)
        form = page.locator("[data-agent-policy-form]").first
        assert form.evaluate(ASSOCIATION_JS) == []
        version = int(form.locator("#id_front_desk-expected_version").input_value())
        expect(form.locator("#id_front_desk-enabled")).not_to_be_checked()
        form.locator("#id_front_desk-autonomy_ceiling").select_option("A1")
        form.locator("#id_front_desk-enabled").check()
        form.locator('input[value="workflows.review_task"]').check()
        click_to_navigate(form.locator("button[type=submit]"))
        expect(form.locator("#id_front_desk-expected_version")).to_have_value(
            str(version + 1)
        )
        capture(page, renewal_artifact_root, f"agents-policy-{theme}", width)
        check_page(
            page,
            renewal_base_url,
            renewal_artifact_root,
            f"agents-policy-{theme}-{width}",
        )
        form.locator("#id_front_desk-timeout_seconds").fill("0")
        click_to_navigate(form.locator("button[type=submit]"))
        expect(form.locator("[role=alert]")).to_be_visible()
        expect(form.locator("#id_front_desk-timeout_seconds")).to_have_attribute(
            "aria-invalid", "true"
        )
        assert form.evaluate(ASSOCIATION_JS) == []
        capture(page, renewal_artifact_root, f"agents-invalid-{theme}", width)
        form.locator("#id_front_desk-timeout_seconds").fill("30")
        form.locator("#id_front_desk-enabled").uncheck()
        click_to_navigate(form.locator("button[type=submit]"))
        expect(form.locator("#id_front_desk-expected_version")).to_have_value(
            str(version + 2)
        )
        expect(form.locator("#id_front_desk-enabled")).not_to_be_checked()
        expect(page.locator("h1")).to_have_text(gettext("Agent policies"))
        if width == 390:
            page.set_viewport_size({"width": 320, "height": 900})
            page.emulate_media(reduced_motion="reduce", forced_colors="active")
            capture(page, renewal_artifact_root, f"agents-reflow-{theme}", 320)
    finally:
        context.close()


def seed_proposal(staff: dict[str, str], manager: dict[str, str]) -> tuple[str, str]:
    environment = {
        **os.environ,
        "APP_DATABASE_URL": staff["dsn"],
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "PYTHONPATH": ".:tests",
    }
    result = subprocess.run(  # noqa: S603 - fixed fixture module and opaque identifiers.
        [
            sys.executable,
            "-m",
            "renewal.agent_proposal_fixture",
            manager["id"],
            staff["clinic_a"],
            staff["organization"],
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    seeded = json.loads(result.stdout)
    return str(UUID(seeded["proposal_id"])), str(seeded["source_revision"])


@pytest.mark.parametrize(
    "presentation",
    [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")],
)
def test_native_exact_effect_approval_and_rejection(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    presentation: tuple[int, str],
) -> None:
    width, theme = presentation
    staff = availability_staff
    manager = seed_ai_admin(staff, theme)
    proposal, source_revision = seed_proposal(staff, manager)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        java_script_enabled=False,
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
    )
    try:
        page = context.new_page()
        sign_in_manager(page, renewal_base_url, staff, manager)
        queue = f"{renewal_base_url}/clinics/{staff['clinic_a']}/automations/agents/"
        goto_settled(page, queue)
        row = page.locator(f'[data-agent-proposal="{proposal}"]')
        expect(row).to_have_attribute("data-agent-state", "validated")
        expect(row.locator("[data-agent-source-revision]")).to_have_text(
            source_revision
        )
        expect(row.locator("[data-agent-task-due]")).to_be_visible()
        digest = row.locator('input[name="digest"]').input_value()
        capture(page, renewal_artifact_root, f"agents-review-{theme}", width)
        row.locator('input[name="digest"]').evaluate(
            "(input) => { input.value = 'f'.repeat(64); }"
        )
        click_to_navigate(row.locator('button[value="approve"]'))
        expect(page.locator('[role="alert"]')).to_be_visible()
        expect(row).to_have_attribute("data-agent-state", "validated")
        expect(row.locator('input[name="digest"]')).to_have_value(digest)
        capture(page, renewal_artifact_root, f"agents-stale-{theme}", width)
        click_to_navigate(row.locator('button[value="approve"]'))
        expect(row).to_have_attribute("data-agent-state", "receipt")
        capture(page, renewal_artifact_root, f"agents-receipt-{theme}", width)
        with psycopg.connect(staff["dsn"]) as connection:
            connection.execute("SET ROLE clinic_app")
            connection.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [staff["organization"]],
            )
            connection.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [manager["id"]]
            )
            assert connection.execute("SELECT current_user").fetchone() == (
                "clinic_app",
            )
            assert connection.execute(
                "SELECT t.kind FROM clinic_app.ai_actionreceipt r "
                "JOIN clinic_app.ai_actionexecution e ON e.id=r.execution_id "
                "JOIN clinic_app.workflows_task t ON t.id=r.provider_reference "
                "WHERE e.proposal_id=%s",
                [proposal],
            ).fetchall() == [("checklist",)]
        rejected, _source_revision = seed_proposal(staff, manager)
        goto_settled(page, queue)
        row = page.locator(f'[data-agent-proposal="{rejected}"]')
        click_to_navigate(row.locator('button[value="reject"]'))
        expect(row).to_have_attribute("data-agent-state", "rejected")
        capture(page, renewal_artifact_root, f"agents-rejected-{theme}", width)
    finally:
        context.close()
