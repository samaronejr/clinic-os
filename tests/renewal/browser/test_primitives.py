"""Primitives suite: every workspace primitive in every state, from real CSS.

The showcase route is DEBUG-only and the supervised runtime serves with
``DEBUG = False``, so the suite first proves the route is absent from the real
server, then renders ``identity/showcase.html`` through Django's template
engine and fulfils it at the real origin. Every stylesheet, script and font
still arrives from the supervised server, so the captures show the bytes the
product ships. All content is synthetic; tracing stays off.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import pytest
from apps.core.templatetags.components import COMPONENT_STATES
from django.template.loader import render_to_string
from django.utils.translation import gettext
from playwright.sync_api import expect

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Page, Route

OK_STATUS = 200
FORBIDDEN_STATUS = 403
NOT_FOUND_STATUS = 404
SHOWCASE_PATH = "/__ui__/auth/"
PRIMITIVES = ("navigation", "field", "action", "status", "table", "panel")
STATES = ("default", "focus", "disabled", "loading", "error", "success")
# Design system v2: the component library, every one with every SC-8 state.
COMPONENTS = (
    "dialog",
    "drawer",
    "combobox",
    "datetime",
    "resource_grid",
    "command_palette",
    "toast",
    "tabs",
    "segmented",
    "editor",
    "provenance",
    "citation",
    "diff",
    "document_viewer",
    "chart",
)
SC8_STATES = tuple(state.key for state in COMPONENT_STATES)
THEMES = ("light", "dark")
DENSITIES = ("comfortable", "compact")
MATRIX_VIEWPORTS = {"1280": (1280, 900), "375": (375, 812), "320": (320, 640)}
AXE_URL = "/static/vendor/axe/axe.min.js"
AXE_BLOCKING = ("serious", "critical")
NON_INTERACTIVE = frozenset({"provenance"})
VIEWPORTS = {
    "mobile-375": (375, 812),
    "tablet-768": (768, 1024),
    "desktop-1280": (1280, 800),
}
REFLOW_VIEWPORTS = {"reflow-320": (320, 640), "zoom-200-proxy-640": (640, 800)}
NAVY = "rgb(15, 45, 58)"
CYAN = "rgb(0, 229, 208)"
# Every surface is navy in the dark theme, so every ring is cyan there.
DARK_RING = CYAN
MIN_RING_PX = 2.0
MIN_CONTRAST = 4.5
MIN_LARGE_CONTRAST = 3.0
MAX_TAB_STOPS = 2000

OVERFLOW_JS = """(root) => {
  const bad = [];
  for (const el of root.querySelectorAll('*')) {
    // Scroll regions scroll by design; visually hidden text is clipped by design.
    const clipped = '.table-scroll, .resource-grid-scroll, .tabs-list,'
      + ' .docviewer-canvas, .visually-hidden, svg';
    if (el.closest(clipped) || el.tagName === 'INPUT') continue;
    if (el.clientWidth === 0) continue;
    if (el.scrollWidth > el.clientWidth + 1) {
      bad.push(el.tagName + '.' + el.className + ' ' +
        el.scrollWidth + '>' + el.clientWidth);
    }
  }
  return bad;
}"""

CONTRAST_JS = """(root) => {
  const parse = (value) => {
    const m = value.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map((s) => parseFloat(s));
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const lin = (c) => {
    c /= 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const background = (el) => {
    let node = el;
    while (node && node !== document.documentElement) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg && bg.a >= 0.99) return bg;
      node = node.parentElement;
    }
    return parse(getComputedStyle(document.body).backgroundColor);
  };
  const rows = [];
  for (const el of root.querySelectorAll('*')) {
    const text = Array.from(el.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent.trim()).join('');
    if (!text) continue;
    if (el.closest('[disabled], [aria-disabled="true"]')) continue;
    if (el.matches('input')) continue;
    if (el.disabled) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const fg = parse(cs.color);
    const bg = background(el);
    if (!fg || !bg) continue;
    const l1 = lum(fg), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight, 10);
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    rows.push({text: text.slice(0, 40), ratio: Math.round(ratio * 100) / 100, large,
               fg: cs.color, bg: `rgb(${bg.r}, ${bg.g}, ${bg.b})`});
  }
  return rows;
}"""

STATUS_JS = """(status) => {
  const cs = getComputedStyle(status);
  const rect = status.getBoundingClientRect();
  return {
    text: (status.textContent || '').trim().slice(0, 40),
    visibility: cs.visibility,
    display: cs.display,
    opacity: cs.opacity,
    width: rect.width,
    height: rect.height,
  };
}"""

FOCUS_JS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const cs = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  const classes = typeof el.className === 'string' && el.className
    ? '.' + el.className.split(' ').join('.') : '';
  const label = (el.id ? '#' + el.id : el.tagName.toLowerCase()) + classes;
  return {
    label,
    text: (el.textContent || el.value || '').trim().slice(0, 40),
    focusVisible: el.matches(':focus-visible'),
    outlineStyle: cs.outlineStyle,
    outlineWidth: cs.outlineWidth,
    outlineColor: cs.outlineColor,
    inNav: Boolean(el.closest('.nav')),
    disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
    // A focused textarea scrolls its caret line into view, not its whole box:
    // require its top edge, both sides and the whole caret line to be visible.
    within: rect.top >= -1 && rect.left >= -1 && rect.right <= window.innerWidth + 1 &&
      (el.tagName === 'TEXTAREA'
        ? rect.top + parseFloat(cs.paddingTop) + parseFloat(cs.lineHeight)
          <= window.innerHeight + 1
        : rect.bottom <= window.innerHeight + 1),
    primitive: (el.closest('[data-primitive]') || {}).dataset?.primitive || null,
    domIndex: Array.from(document.querySelectorAll('*')).indexOf(el),
  };
}"""

TABBABLE_COUNT_JS = """() => {
  const selector = 'a[href], button, input, select, textarea, summary, [tabindex]';
  return Array.from(document.querySelectorAll(selector)).filter((el) => {
    if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true') {
      return false;
    }
    if (el.type === 'hidden') return false;
    if (el.tabIndex < 0) return false;
    // A radio group is one tab stop: its checked radio, else its first one.
    if (el.type === 'radio') {
      const group = Array.from(document.getElementsByName(el.name));
      const stop = group.find((radio) => radio.checked) || group[0];
      if (el !== stop) return false;
    }
    // Closed dialogs, hidden panels and collapsed popups are not in the order.
    return el.checkVisibility({visibilityProperty: true});
  }).length;
}"""


def _px(value: str) -> float:
    match = re.match(r"([0-9.]+)px", value)
    return float(match.group(1)) if match else 0.0


@dataclass(frozen=True)
class Showcase:
    """Everything a primitives test needs: browser, origin, markup, outputs."""

    browser: Browser
    base_url: str
    html: str
    root: Path
    report: dict[str, Any]


@pytest.fixture(scope="session")
def showcase(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> Iterator[Showcase]:
    """Render the DEBUG-only showcase through Django's own template engine."""
    html = render_to_string("identity/showcase.html")
    assert "otpauth://" not in html
    assert "data:image/png" not in html
    browser = renewal_page.context.browser
    assert browser is not None
    root = renewal_artifact_root / "primitives"
    root.mkdir(mode=0o700, exist_ok=True)
    report: dict[str, Any] = {"schema_version": 1, "checks": []}
    yield Showcase(browser, renewal_base_url, html, root, report)
    destination = root / "primitives-report.json"
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)


def _record(report: dict[str, Any], **entry: Any) -> None:  # noqa: ANN401
    checks = report["checks"]
    assert isinstance(checks, list)
    checks.append(entry)


def _save(page: Page, destination: Path, *, full_page: bool) -> str:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(page.screenshot(full_page=full_page))
    destination.chmod(0o600)
    return str(destination)


def _open_showcase(
    showcase: Showcase,
    viewport: tuple[int, int],
    *,
    forced_colors: Literal["active", "none"] | None = None,
    reduced_motion: Literal["reduce", "no-preference"] | None = None,
) -> tuple[BrowserContext, Page]:
    context = showcase.browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        forced_colors=forced_colors,
        reduced_motion=reduced_motion,
    )

    def fulfil(route: Route) -> None:
        route.fulfill(
            status=OK_STATUS,
            content_type="text/html; charset=utf-8",
            body=showcase.html,
        )

    context.route(f"{showcase.base_url}{SHOWCASE_PATH}", fulfil)
    page = context.new_page()
    page.set_default_timeout(20_000)
    with page.expect_response(f"{showcase.base_url}/static/css/clinic-os.css") as css:
        page.goto(f"{showcase.base_url}{SHOWCASE_PATH}", wait_until="load")
    assert css.value.status == OK_STATUS
    # The served stylesheet painted the page: brand tokens resolve even when
    # forced colors replace the rendered colors.
    teal = page.evaluate(
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue('--brand-teal-deep').trim()"
    )
    assert teal == "#007a87"
    if forced_colors != "active":
        button_color = page.evaluate(
            "getComputedStyle(document.querySelector('.actions button'))"
            ".backgroundColor"
        )
        assert button_color == "rgb(0, 122, 135)"
    return context, page


def test_showcase_is_absent_from_the_served_runtime_and_css_is_served(
    renewal_page: Page,
    renewal_owner: dict[str, str],
    showcase: Showcase,
) -> None:
    # Anonymous: the tenant middleware fails closed before URL resolution.
    anonymous = renewal_page.request.get(f"{showcase.base_url}{SHOWCASE_PATH}")
    assert anonymous.status == FORBIDDEN_STATUS
    # Signed in: the production resolver has no showcase route at all.
    renewal_page.goto(f"{showcase.base_url}/auth/login/", wait_until="load")
    renewal_page.fill("#id_username", renewal_owner["username"])
    renewal_page.fill("#id_password", renewal_owner["password"])
    with renewal_page.expect_navigation(wait_until="load"):
        renewal_page.click("button[type=submit]")
    assert any(
        cookie["name"] == "sessionid" for cookie in renewal_page.context.cookies()
    )
    absent = renewal_page.request.get(f"{showcase.base_url}{SHOWCASE_PATH}")
    assert absent.status == NOT_FOUND_STATUS
    css = renewal_page.request.get(f"{showcase.base_url}/static/css/clinic-os.css")
    assert css.status == OK_STATUS
    body = css.text()
    assert "--color-primary: var(--brand-teal-deep)" in body
    assert "--color-focus-on-dark: var(--brand-cyan)" in body
    _record(
        showcase.report,
        assertion="showcase 403 anonymous and 404 signed in on the DEBUG=False"
        " runtime; clinic-os.css served with the brand tokens",
        anonymous_status=anonymous.status,
        signed_in_status=absent.status,
        css_status=css.status,
    )


@pytest.mark.parametrize("viewport", sorted(VIEWPORTS))
def test_every_primitive_state_renders_without_overflow_or_low_contrast(
    viewport: str,
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(showcase, VIEWPORTS[viewport])
    try:
        scroll_width = page.evaluate("document.scrollingElement.scrollWidth")
        inner_width = page.evaluate("window.innerWidth")
        assert scroll_width <= inner_width
        full = _save(page, showcase.root / viewport / "full-page.png", full_page=True)
        captures: dict[str, str] = {}
        worst: dict[str, float] = {}
        for primitive in PRIMITIVES:
            for state in STATES:
                block = page.locator(
                    f'[data-primitive="{primitive}"][data-state="{state}"]'
                )
                assert block.count() == 1, (primitive, state)
                block.scroll_into_view_if_needed()
                assert block.is_visible()
                heading = block.locator("h3").inner_text().strip().lower()
                assert heading == gettext(state.capitalize()).lower()
                overflow = block.evaluate(OVERFLOW_JS)
                assert overflow == [], (viewport, primitive, state, overflow)
                rows = block.evaluate(CONTRAST_JS)
                assert rows, (primitive, state)
                for row in rows:
                    floor = MIN_LARGE_CONTRAST if row["large"] else MIN_CONTRAST
                    assert row["ratio"] >= floor, (viewport, primitive, state, row)
                worst[f"{primitive}-{state}"] = min(row["ratio"] for row in rows)
                destination = showcase.root / viewport / f"{primitive}-{state}.png"
                destination.write_bytes(block.screenshot())
                destination.chmod(0o600)
                captures[f"{primitive}-{state}"] = str(destination)
        assert len(captures) == len(PRIMITIVES) * len(STATES)
        _record(
            showcase.report,
            assertion="36 primitive states visible, unclipped, contrast >= AA",
            viewport=viewport,
            full_page=full,
            captures=captures,
            lowest_contrast=worst,
            page_scroll_width=scroll_width,
            inner_width=inner_width,
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", sorted(VIEWPORTS))
def test_every_loading_state_shows_its_status_message(
    viewport: str,
    showcase: Showcase,
) -> None:
    # htmx injects `.htmx-indicator { visibility: hidden }` into <head> after
    # the stylesheet; the busy container alone must reveal the message.
    context, page = _open_showcase(showcase, VIEWPORTS[viewport])
    try:
        assert page.evaluate(
            "Array.from(document.head.querySelectorAll('style'))"
            ".some((s) => s.textContent.includes('.htmx-indicator{opacity:0'))"
        )
        readings: dict[str, dict[str, Any]] = {}
        for primitive in PRIMITIVES:
            block = page.locator(
                f'[data-primitive="{primitive}"][data-state="loading"]'
            )
            block.scroll_into_view_if_needed()
            status = block.locator('[role="status"]').first
            assert status.count() == 1, primitive
            assert status.is_visible(), primitive
            reading = status.evaluate(STATUS_JS)
            assert reading["text"], (primitive, reading)
            assert reading["visibility"] == "visible", (primitive, reading)
            assert reading["display"] != "none", (primitive, reading)
            assert float(reading["opacity"]) == 1.0, (primitive, reading)
            assert reading["width"] > 0, (primitive, reading)
            assert reading["height"] > 0, (primitive, reading)
            readings[primitive] = reading
        _record(
            showcase.report,
            assertion="every loading block's role=status message is rendered visible"
            " (visibility, display, opacity, box, text) with htmx's injected"
            " indicator style present",
            viewport=viewport,
            statuses=readings,
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", sorted(REFLOW_VIEWPORTS))
def test_reflow_keeps_one_column_without_horizontal_scroll(
    viewport: str,
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(showcase, REFLOW_VIEWPORTS[viewport])
    try:
        scroll_width = page.evaluate("document.scrollingElement.scrollWidth")
        inner_width = page.evaluate("window.innerWidth")
        assert scroll_width <= inner_width
        columns = page.evaluate(
            "getComputedStyle(document.querySelector('#field .spec-grid'))"
            ".gridTemplateColumns.split(' ').length"
        )
        assert columns == 1
        overflow = page.evaluate(OVERFLOW_JS, page.locator("main").element_handle())
        assert overflow == []
        full = _save(page, showcase.root / viewport / "full-page.png", full_page=True)
        _record(
            showcase.report,
            assertion="single column, no page overflow at reflow width",
            viewport=viewport,
            grid_columns=columns,
            page_scroll_width=scroll_width,
            full_page=full,
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", ["mobile-375", "desktop-1280"])
def test_keyboard_reaches_every_control_with_a_visible_unobscured_ring(
    viewport: str,
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(showcase, VIEWPORTS[viewport])
    try:
        expected = page.evaluate(TABBABLE_COUNT_JS)
        assert expected > 0
        stops: list[dict[str, Any]] = []
        captured: dict[str, str] = {}
        seen_first = False
        for _ in range(MAX_TAB_STOPS):
            page.keyboard.press("Tab")
            info = page.evaluate(FOCUS_JS)
            if info is None:
                # Focus left the document: the end of the tab sequence.
                break
            if stops and info["label"] == stops[0]["label"] and seen_first:
                break
            seen_first = True
            assert info["focusVisible"], info
            assert info["outlineStyle"] == "solid", info
            assert _px(info["outlineWidth"]) >= MIN_RING_PX, info
            assert info["outlineColor"] == (CYAN if info["inNav"] else NAVY), info
            assert not info["disabled"], info
            assert info["within"], info
            if stops:
                assert info["domIndex"] > stops[-1]["domIndex"], (stops[-1], info)
            stops.append(info)
            primitive = info["primitive"]
            if primitive and primitive not in captured:
                captured[primitive] = _save(
                    page,
                    showcase.root / "keyboard" / viewport / f"{primitive}-focus.png",
                    full_page=False,
                )
        assert len(stops) == expected, (len(stops), expected)
        assert set(captured) == set(PRIMITIVES) | (set(COMPONENTS) - NON_INTERACTIVE)
        _record(
            showcase.report,
            assertion="tab order = DOM order; every stop focus-visible, ring >= 2px,"
            " brand ring color, unobscured, no disabled stop",
            viewport=viewport,
            tab_stops=len(stops),
            tabbable_elements=expected,
            captures=captured,
            first_stop=stops[0]["label"],
            last_stop=stops[-1]["label"],
        )
    finally:
        context.close()


def test_error_summary_is_focusable_and_links_move_focus_to_invalid_fields(
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(showcase, VIEWPORTS["mobile-375"])
    try:
        summary = page.locator("#stress-summary")
        assert summary.get_attribute("role") == "alert"
        assert summary.get_attribute("tabindex") == "-1"
        page.keyboard.press("Tab")
        summary.focus()
        info = page.evaluate(FOCUS_JS)
        assert info is not None
        assert info["label"].startswith("#stress-summary")
        assert info["outlineStyle"] == "solid"
        assert _px(info["outlineWidth"]) >= MIN_RING_PX
        capture = _save(
            page, showcase.root / "failure" / "error-summary-focus.png", full_page=False
        )
        links = summary.locator("a")
        targets: list[str] = []
        for index in range(links.count()):
            link = links.nth(index)
            target = (link.get_attribute("href") or "").lstrip("#")
            assert target
            link.click()
            active = page.evaluate("document.activeElement.id")
            assert active == target
            field = page.locator(f"#{target}")
            assert field.get_attribute("aria-invalid") == "true"
            described = field.get_attribute("aria-describedby") or ""
            message = page.locator(f"#{described}").inner_text().strip()
            assert message
            targets.append(target)
        assert len(targets) == 3
        _record(
            showcase.report,
            assertion="error summary focusable with ring; each link focuses its"
            " aria-invalid field whose aria-describedby names the fix",
            capture=capture,
            targets=targets,
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", ["reflow-320", "mobile-375", "desktop-1280"])
def test_long_labels_wrap_instead_of_clipping(
    viewport: str,
    showcase: Showcase,
) -> None:
    size = {**VIEWPORTS, **REFLOW_VIEWPORTS}[viewport]
    context, page = _open_showcase(showcase, size)
    try:
        block = page.locator('[data-stress="long-labels"]')
        block.scroll_into_view_if_needed()
        overflow = block.evaluate(OVERFLOW_JS)
        assert overflow == [], overflow
        assert page.evaluate("document.scrollingElement.scrollWidth") <= size[0]
        metrics = block.evaluate(
            """(root) => {
              const button = root.querySelector('button');
              const label = root.querySelector('label');
              const nav = root.querySelector('.nav');
              const region = root.querySelector('.table-scroll');
              const badge = root.querySelector('.badge');
              const lines = (el) => Math.round(el.getBoundingClientRect().height /
                parseFloat(getComputedStyle(el).lineHeight));
              return {
                buttonLines: lines(button),
                buttonClipped: button.scrollHeight > button.clientHeight + 1,
                labelClipped: label.scrollWidth > label.clientWidth + 1,
                navClipped: nav.scrollWidth > nav.clientWidth + 1,
                badgeClipped: badge.scrollWidth > badge.clientWidth + 1,
                regionScrolls: region.scrollWidth > region.clientWidth,
                regionFocusable: region.tabIndex === 0 &&
                  region.getAttribute('role') === 'region',
                regionLabelled: Boolean(document.getElementById(
                  region.getAttribute('aria-labelledby') || '')),
              };
            }"""
        )
        assert not metrics["buttonClipped"]
        assert not metrics["labelClipped"]
        assert not metrics["navClipped"]
        assert not metrics["badgeClipped"]
        assert metrics["regionFocusable"]
        assert metrics["regionLabelled"]
        destination = showcase.root / "failure" / f"long-labels-{viewport}.png"
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        destination.write_bytes(block.screenshot())
        destination.chmod(0o600)
        _record(
            showcase.report,
            assertion="long labels wrap; nothing clips; unbroken table value scrolls"
            " inside a labelled focusable region instead of the page",
            viewport=viewport,
            capture=str(destination),
            metrics=metrics,
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", ["mobile-375", "desktop-1280"])
def test_forced_colors_keeps_controls_focus_and_status_visible(
    viewport: str,
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(
        showcase, VIEWPORTS[viewport], forced_colors="active"
    )
    try:
        assert page.evaluate("matchMedia('(forced-colors: active)').matches")
        borders = page.evaluate(
            """() => {
              const selectors = [
                '[data-primitive="field"][data-state="default"] input',
                '[data-primitive="action"][data-state="default"] button',
                '[data-primitive="action"][data-state="disabled"] button',
                '[data-primitive="status"][data-state="default"] .badge',
                '[data-primitive="status"][data-state="error"] .feedback',
                '[data-primitive="table"][data-state="loading"] .skeleton',
                '[data-primitive="panel"][data-state="default"] .panel',
                '[data-primitive="navigation"][data-state="error"] .nav-badge',
              ];
              return selectors.map((s) => {
                const el = document.querySelector(s);
                const cs = getComputedStyle(el);
                return {selector: s, style: cs.borderTopStyle, width: cs.borderTopWidth,
                        color: cs.color, background: cs.backgroundColor};
              });
            }"""
        )
        for row in borders:
            assert row["style"] != "none", row
            assert _px(row["width"]) >= 1.0, row
        enabled = borders[1]["color"]
        disabled = borders[2]["color"]
        assert enabled != disabled, (enabled, disabled)
        current = page.evaluate(
            "getComputedStyle(document.querySelector('.nav-link[aria-current=\"page\"]'))"
            ".textDecorationLine"
        )
        assert "underline" in current
        nav_colors = page.evaluate(
            """() => {
              const link = document.querySelector('.nav-link');
              return {text: getComputedStyle(link).color,
                      surface: getComputedStyle(link.closest('.nav')).backgroundColor};
            }"""
        )
        assert nav_colors["text"] != nav_colors["surface"]
        page.locator('[data-primitive="action"][data-state="focus"] button').focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        info = page.evaluate(FOCUS_JS)
        assert info is not None
        assert info["outlineStyle"] == "solid"
        assert _px(info["outlineWidth"]) >= MIN_RING_PX
        capture = _save(
            page,
            showcase.root / "failure" / f"forced-colors-{viewport}.png",
            full_page=True,
        )
        _record(
            showcase.report,
            assertion="forced colors: borders on every control and surface, current"
            " item underlined, disabled distinguishable, focus ring solid",
            viewport=viewport,
            capture=capture,
            borders=borders,
            focus=info,
        )
    finally:
        context.close()


def test_reduced_motion_removes_transitions_and_the_press_transform(
    showcase: Showcase,
) -> None:
    selector = '[data-primitive="action"][data-state="focus"] button'
    context, page = _open_showcase(showcase, VIEWPORTS["desktop-1280"])
    try:
        button = page.locator(selector)
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_function(
            "(s) => getComputedStyle(document.querySelector(s)).transform"
            " === 'matrix(1, 0, 0, 1, 0, 1)'",
            arg=selector,
            timeout=2_000,
        )
        pressed = page.evaluate(
            f"getComputedStyle(document.querySelector('{selector}'))"
        )
        page.mouse.up()
        assert pressed["transitionDuration"].startswith("0.12s")
    finally:
        context.close()
    context, page = _open_showcase(
        showcase, VIEWPORTS["desktop-1280"], reduced_motion="reduce"
    )
    try:
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        durations = page.evaluate(
            """() => ['button', 'input', '.nav-link', '.panel'].map((s) =>
              [s, getComputedStyle(document.querySelector(s)).transitionDuration])"""
        )
        for selector_name, duration in durations:
            assert duration == "0s", (selector_name, duration)
        button = page.locator(selector)
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        pressed = page.evaluate(
            f"getComputedStyle(document.querySelector('{selector}'))"
        )
        capture = _save(
            page,
            showcase.root / "failure" / "reduced-motion-press.png",
            full_page=False,
        )
        page.mouse.up()
        assert pressed["transform"] == "none"
        assert (
            page.evaluate(
                "getComputedStyle(document.querySelector('.skeleton')).animationName"
            )
            == "none"
        )
        _record(
            showcase.report,
            assertion="reduced motion: 0s transitions, no press transform, no"
            " animation; default motion shows the 1px press and 120ms transition",
            capture=capture,
            durations=durations,
        )
    finally:
        context.close()


# --------------------------------------------------------------------------
# Design system v2: component library matrix, axe, themes, density, behavior
# --------------------------------------------------------------------------

TEXT_OVERLAP_JS = """(root) => {
  const boxes = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.textContent.trim()) continue;
    const el = node.parentElement;
    if (el.closest('.visually-hidden, [hidden], svg, .nav-wordmark')) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const rect of range.getClientRects()) {
      if (rect.width < 1 || rect.height < 1) continue;
      boxes.push({el, rect, text: node.textContent.trim().slice(0, 30)});
    }
  }
  const clip = (el) => {
    let out = {left: -Infinity, top: -Infinity, right: Infinity, bottom: Infinity};
    for (let a = el.parentElement; a; a = a.parentElement) {
      const o = getComputedStyle(a);
      if (o.overflowX !== 'visible' || o.overflowY !== 'visible') {
        const r = a.getBoundingClientRect();
        out = {left: Math.max(out.left, r.left), top: Math.max(out.top, r.top),
               right: Math.min(out.right, r.right),
               bottom: Math.min(out.bottom, r.bottom)};
      }
    }
    return out;
  };
  const visible = boxes.map((b) => {
    const c = clip(b.el);
    const r = {left: Math.max(b.rect.left, c.left), top: Math.max(b.rect.top, c.top),
               right: Math.min(b.rect.right, c.right),
               bottom: Math.min(b.rect.bottom, c.bottom)};
    return {...b, r};
  }).filter((b) => b.r.right - b.r.left > 1 && b.r.bottom - b.r.top > 1);
  const overlaps = [];
  for (let i = 0; i < visible.length; i++) {
    for (let j = i + 1; j < visible.length; j++) {
      const a = visible[i], b = visible[j];
      if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
      const h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
      if (w > 2 && h > 2) overlaps.push([a.text, b.text, Math.round(w), Math.round(h)]);
    }
  }
  return overlaps.slice(0, 10);
}"""

AXE_RUN_JS = """async () => {
  const result = await axe.run(document, {
    runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
      'wcag22aa', 'best-practice']},
    resultTypes: ['violations'],
  });
  return result.violations.map((v) => ({
    id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
    count: v.nodes.length,
  }));
}"""


def _apply(page: Page, theme: str, density: str) -> None:
    """Set the preference attributes the shell renders for a signed-in user."""
    page.evaluate(
        "([theme, density]) => {"
        " document.documentElement.dataset.theme = theme;"
        " document.documentElement.dataset.density = density; }",
        [theme, density],
    )
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == (
        "rgb(11, 31, 40)" if theme == "dark" else "rgb(247, 245, 240)"
    )


def _write_json(destination: Path, payload: object) -> str:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)
    return str(destination)


@pytest.mark.parametrize("viewport", sorted(MATRIX_VIEWPORTS))
@pytest.mark.parametrize("density", DENSITIES)
@pytest.mark.parametrize("theme", THEMES)
def test_every_component_state_in_every_theme_density_and_width(
    theme: str,
    density: str,
    viewport: str,
    showcase: Showcase,
) -> None:
    size = MATRIX_VIEWPORTS[viewport]
    context, page = _open_showcase(showcase, size)
    try:
        _apply(page, theme, density)
        assert page.evaluate("document.scrollingElement.scrollWidth") <= size[0]
        folder = showcase.root / "matrix" / f"{theme}-{density}-{viewport}"
        captures = 0
        worst: dict[str, float] = {}
        for name in (*COMPONENTS, *PRIMITIVES):
            for state in SC8_STATES:
                block = page.locator(f'[data-primitive="{name}"][data-state="{state}"]')
                assert block.count() == 1, (name, state)
                block.scroll_into_view_if_needed()
                assert block.is_visible(), (name, state)
                overflow = block.evaluate(OVERFLOW_JS)
                assert overflow == [], (theme, density, viewport, name, state, overflow)
                rows = block.evaluate(CONTRAST_JS)
                assert rows, (name, state)
                for row in rows:
                    floor = MIN_LARGE_CONTRAST if row["large"] else MIN_CONTRAST
                    assert row["ratio"] >= floor, (
                        theme,
                        density,
                        viewport,
                        name,
                        state,
                        row,
                    )
                worst[f"{name}-{state}"] = min(row["ratio"] for row in rows)
                destination = folder / f"{name}-{state}.png"
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_bytes(block.screenshot())
                destination.chmod(0o600)
                captures += 1
        assert captures == (len(COMPONENTS) + len(PRIMITIVES)) * len(SC8_STATES)
        _record(
            showcase.report,
            assertion="every component x SC-8 state visible, unclipped, contrast >= AA",
            theme=theme,
            density=density,
            viewport=viewport,
            captures=captures,
            folder=str(folder),
            lowest_contrast=min(worst.values()),
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", sorted(MATRIX_VIEWPORTS))
@pytest.mark.parametrize("density", DENSITIES)
@pytest.mark.parametrize("theme", THEMES)
def test_axe_finds_no_serious_or_critical_violation(
    theme: str,
    density: str,
    viewport: str,
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(showcase, MATRIX_VIEWPORTS[viewport])
    try:
        _apply(page, theme, density)
        with page.expect_response(f"{showcase.base_url}{AXE_URL}") as axe_response:
            page.add_script_tag(url=f"{showcase.base_url}{AXE_URL}")
        assert axe_response.value.status == OK_STATUS
        violations = page.evaluate(AXE_RUN_JS)
        report = _write_json(
            showcase.root / "axe" / f"{theme}-{density}-{viewport}.json", violations
        )
        blocking = [v for v in violations if v["impact"] in AXE_BLOCKING]
        assert blocking == [], blocking
        _record(
            showcase.report,
            assertion="axe-core 4.13.0 (wcag2a/aa, wcag21a/aa, wcag22aa,"
            " best-practice):"
            " 0 serious or critical violations",
            theme=theme,
            density=density,
            viewport=viewport,
            axe_report=report,
            nonblocking=[(v["id"], v["impact"], v["count"]) for v in violations],
        )
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_dark_and_compact_keep_rings_targets_and_tokens(
    theme: str,
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(showcase, MATRIX_VIEWPORTS["1280"])
    try:
        _apply(page, theme, "compact")
        ring = NAVY if theme == "light" else DARK_RING
        page.locator('[data-primitive="action"][data-state="focus"] button').focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        info = page.evaluate(FOCUS_JS)
        assert info is not None
        assert info["outlineColor"] == ring, info
        # Compact tightens cells but no target shrinks below 44px.
        targets = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                 'main button:not(.datetime-day), main .button,'
                 + ' main input:not([type=radio]),'
                 + ' main .citation, main .tab, main .segmented-option'))
               .filter((el) => el.getClientRects().length && !el.closest('[hidden]'))
               .map((el) => [el.className || el.tagName,
                             Math.round(el.getBoundingClientRect().height)])"""
        )
        small = [target for target in targets if target[1] < 44]
        assert small == [], small[:5]
        padding = page.evaluate(
            "getComputedStyle(document.querySelector('.table td')).paddingTop"
        )
        assert padding == "4px"
        _record(
            showcase.report,
            assertion="compact density keeps every target >= 44px; focus ring is"
            " navy on paper and cyan on the navy dark theme",
            theme=theme,
            ring=info["outlineColor"],
            targets_checked=len(targets),
            compact_cell_padding=padding,
        )
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_zoom_200_with_pt_br_strings_never_overlaps_text(
    theme: str,
    showcase: Showcase,
) -> None:
    context = showcase.browser.new_context(
        viewport={"width": 640, "height": 400},
        device_scale_factor=2,
        locale="pt-BR",
    )

    def fulfil(route: Route) -> None:
        route.fulfill(
            status=OK_STATUS,
            content_type="text/html; charset=utf-8",
            body=showcase.html,
        )

    context.route(f"{showcase.base_url}{SHOWCASE_PATH}", fulfil)
    page = context.new_page()
    try:
        page.goto(f"{showcase.base_url}{SHOWCASE_PATH}", wait_until="load")
        _apply(page, theme, "comfortable")
        assert page.evaluate("document.scrollingElement.scrollWidth") <= 640
        overlaps: dict[str, Any] = {}
        for spec in page.locator("[data-primitive]").all():
            found = spec.evaluate(TEXT_OVERLAP_JS)
            if found:
                name = spec.get_attribute("data-primitive")
                key = f"{name}-{spec.get_attribute('data-state')}"
                overlaps[key] = found
        assert overlaps == {}, overlaps
        capture = _save(
            page, showcase.root / "zoom-200" / f"{theme}-full-page.png", full_page=True
        )
        _record(
            showcase.report,
            assertion="200% zoom (640 CSS px at DPR 2) with pt-BR copy: no page"
            " overflow and no overlapping text in any specimen",
            theme=theme,
            capture=capture,
        )
    finally:
        context.close()


def test_forced_colors_and_reduced_motion_keep_component_states_visible(
    showcase: Showcase,
) -> None:
    context, page = _open_showcase(
        showcase,
        MATRIX_VIEWPORTS["1280"],
        forced_colors="active",
        reduced_motion="reduce",
    )
    try:
        borders = page.evaluate(
            """() => ['.state-note', '.dialog', '.drawer', '.combobox-listbox',
                      '.resource-grid-scroll', '.toast', '.segmented-options',
                      '.editor',
                      '.provenance', '.citation', '.diff', '.docviewer', '.chart']
              .map((s) => { const el = document.querySelector(s);
                const cs = getComputedStyle(el);
                return [s, cs.borderTopStyle, cs.borderTopWidth]; })"""
        )
        for selector, style, width in borders:
            assert style != "none", selector
            assert _px(width) >= 1.0, selector
        checked = page.evaluate(
            "getComputedStyle(document.querySelector("
            "'.segmented-option:has(input:checked)')).borderTopWidth"
        )
        assert _px(checked) >= MIN_RING_PX
        durations = page.evaluate(
            """() => ['.toast', '.segmented-option', '.tab', '.citation',
                      '.resource-slot']
              .map((s) => getComputedStyle(document.querySelector(s))
                .transitionDuration)"""
        )
        assert set(durations) == {"0s"}
        page.locator('[data-primitive="tabs"][data-state="default"] .tab').first.focus()
        info = page.evaluate(FOCUS_JS)
        assert info is not None
        assert info["outlineStyle"] == "solid"
        for name in ("combobox", "datetime", "segmented", "tabs", "citation", "chart"):
            block = page.locator(f'[data-primitive="{name}"][data-state="focus"]')
            block.scroll_into_view_if_needed()
            _save(
                page,
                showcase.root / "forced-colors" / f"{name}-focus.png",
                full_page=False,
            )
        capture = _save(
            page, showcase.root / "forced-colors" / "full-page.png", full_page=True
        )
        _record(
            showcase.report,
            assertion="forced colors + reduced motion: component boundaries, checked"
            " segment and focus ring visible; every component transition 0s",
            capture=capture,
            durations=durations,
        )
    finally:
        context.close()


GRID_INDEX_JS = (
    "Array.from(document.querySelectorAll('#resource_grid-default [role=gridcell]'))"
    ".indexOf(document.activeElement)"
)


@pytest.fixture
def live_page(showcase: Showcase) -> Iterator[Page]:
    """Open the showcase at desktop width for one keyboard journey."""
    context, page = _open_showcase(showcase, MATRIX_VIEWPORTS["1280"])
    try:
        yield page
    finally:
        context.close()


def test_dialog_opens_modally_and_returns_focus(
    live_page: Page, showcase: Showcase
) -> None:
    opener = live_page.locator('[data-dialog-open="live-dialog"]')
    opener.focus()
    live_page.keyboard.press("Enter")
    dialog = live_page.locator("#live-dialog")
    expect(dialog).to_have_attribute("open", "")
    assert live_page.evaluate("document.activeElement.closest('#live-dialog') !== null")
    capture = _save(
        live_page, showcase.root / "behaviors" / "dialog-open.png", full_page=False
    )
    live_page.keyboard.press("Escape")
    expect(dialog).not_to_have_attribute("open", "")
    assert live_page.evaluate("document.activeElement.dataset.dialogOpen") == (
        "live-dialog"
    )
    _record(
        showcase.report,
        assertion="dialog: modal, Escape closes, focus returns",
        capture=capture,
    )


def test_drawer_is_non_modal_and_escape_returns_focus(
    live_page: Page, showcase: Showcase
) -> None:
    drawer = live_page.locator("#live-drawer")
    toggle = live_page.locator('[data-drawer-toggle="live-drawer"]')
    expect(drawer).to_be_hidden()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.focus()
    live_page.keyboard.press("Enter")
    expect(drawer).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    live_page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    assert live_page.evaluate("document.activeElement.dataset.drawerToggle") == (
        "live-drawer"
    )
    _record(showcase.report, assertion="drawer: toggle, Escape, focus return")


def test_combobox_filters_moves_and_chooses(
    live_page: Page, showcase: Showcase
) -> None:
    combo = live_page.locator("#live-combobox")
    listbox = live_page.locator("#live-combobox-listbox")
    combo.focus()
    live_page.keyboard.type("ans")
    expect(listbox).to_be_visible()
    expect(combo).to_have_attribute("aria-expanded", "true")
    expect(listbox.locator("[role=option]:visible")).to_have_count(1)
    live_page.keyboard.press("ArrowDown")
    expect(combo).to_have_attribute("aria-activedescendant", "live-combobox-opt-3")
    expect(live_page.locator("#live-combobox-opt-3")).to_have_attribute(
        "aria-selected", "true"
    )
    live_page.keyboard.press("Enter")
    expect(combo).to_have_value("Anselmo Prado Sintético")
    expect(listbox).to_be_hidden()
    expect(live_page.locator('input[name="live-combobox_choice"]')).to_have_value("p3")
    live_page.keyboard.press("Escape")
    expect(combo).to_have_value("")
    _record(showcase.report, assertion="combobox: filter, arrows, Enter, Escape")


def test_date_picker_moves_by_day_and_month_and_respects_bounds(
    live_page: Page, showcase: Showcase
) -> None:
    toggle = live_page.locator('[aria-controls="live-datetime-calendar"]')
    expect(toggle).to_be_visible()
    toggle.focus()
    live_page.keyboard.press("Enter")
    calendar = live_page.locator("#live-datetime-calendar")
    expect(calendar).to_be_visible()
    active_date = "document.activeElement.dataset.date"
    assert live_page.evaluate(active_date) == "2031-03-04"
    live_page.keyboard.press("ArrowLeft")
    live_page.keyboard.press("ArrowLeft")
    assert live_page.evaluate(active_date) == "2031-03-02"
    assert (
        live_page.evaluate("document.activeElement.getAttribute('aria-disabled')")
        == "true"
    )
    live_page.keyboard.press("Enter")
    expect(calendar).to_be_visible()
    for _ in range(3):
        live_page.keyboard.press("ArrowRight")
    live_page.keyboard.press("PageDown")
    assert live_page.evaluate(active_date) == "2031-04-05"
    capture = _save(
        live_page, showcase.root / "behaviors" / "calendar.png", full_page=False
    )
    live_page.keyboard.press("Enter")
    expect(live_page.locator("#live-datetime-date")).to_have_value("05/04/2031")
    expect(calendar).to_be_hidden()
    assert live_page.evaluate(
        "document.activeElement.hasAttribute('data-datetime-toggle')"
    )
    _record(
        showcase.report,
        assertion="date picker: arrows, PageDown, bounds, focus",
        capture=capture,
    )


def test_resource_grid_has_one_tab_stop_and_arrow_navigation(
    live_page: Page, showcase: Showcase
) -> None:
    grid = live_page.locator("#resource_grid-default")
    assert grid.locator('[role=gridcell][tabindex="0"]').count() == 1
    grid.locator("[role=gridcell]").first.focus()
    live_page.keyboard.press("ArrowRight")
    assert live_page.evaluate(GRID_INDEX_JS) == 1
    live_page.keyboard.press("ArrowDown")
    live_page.keyboard.press("End")
    assert live_page.evaluate(GRID_INDEX_JS) == 5
    assert grid.locator('[role=gridcell][tabindex="0"]').count() == 1
    _record(showcase.report, assertion="resource grid: roving tab stop and arrows")


def test_tabs_select_with_arrows_home_and_end(
    live_page: Page, showcase: Showcase
) -> None:
    tabs = live_page.locator('[data-tabs="tabs-default"] [role=tab]')
    tabs.first.focus()
    live_page.keyboard.press("ArrowRight")
    expect(tabs.nth(1)).to_have_attribute("aria-selected", "true")
    expect(live_page.locator("#tabs-default-timeline")).to_be_visible()
    expect(live_page.locator("#tabs-default-overview")).to_be_hidden()
    live_page.keyboard.press("End")
    expect(tabs.last).to_have_attribute("aria-selected", "true")
    live_page.keyboard.press("Home")
    expect(tabs.first).to_have_attribute("aria-selected", "true")
    _record(showcase.report, assertion="tabs: arrows, Home, End, panels follow")


def test_announcer_speaks_and_toast_waits_for_dismissal(
    live_page: Page, showcase: Showcase
) -> None:
    live_page.locator("[data-announce]").click()
    expect(live_page.locator('[data-announcer="polite"]')).to_have_text(
        gettext("Reminder confirmed.")
    )
    toasts = live_page.locator("#behaviors [data-toast-region] .toast")
    expect(toasts).to_have_count(1)
    toasts.first.locator("[data-toast-dismiss]").click()
    expect(toasts).to_have_count(0)
    _record(showcase.report, assertion="announcer: polite text, dismissible toast")


def test_command_palette_opens_on_ctrl_k_and_closes_on_escape(
    live_page: Page, showcase: Showcase
) -> None:
    live_page.locator("#showcase-title").click()
    live_page.keyboard.press("Control+k")
    palette = live_page.locator("#live-palette")
    expect(palette).to_have_attribute("open", "")
    expect(live_page.locator("#live-palette-input")).to_be_focused()
    live_page.keyboard.type("agen")
    expect(
        live_page.locator("#live-palette-input-listbox [role=option]:visible")
    ).to_have_count(2)
    capture = _save(
        live_page, showcase.root / "behaviors" / "palette.png", full_page=False
    )
    live_page.keyboard.press("Escape")
    live_page.keyboard.press("Escape")
    expect(palette).not_to_have_attribute("open", "")
    _record(
        showcase.report,
        assertion="command palette: Ctrl+K, filter, Escape x2",
        capture=capture,
    )


def test_components_keep_their_native_baseline_without_javascript(
    showcase: Showcase,
) -> None:
    context = showcase.browser.new_context(
        viewport={"width": 1280, "height": 900}, java_script_enabled=False
    )

    def fulfil(route: Route) -> None:
        route.fulfill(
            status=OK_STATUS,
            content_type="text/html; charset=utf-8",
            body=showcase.html,
        )

    context.route(f"{showcase.base_url}{SHOWCASE_PATH}", fulfil)
    page = context.new_page()
    try:
        page.goto(f"{showcase.base_url}{SHOWCASE_PATH}", wait_until="load")
        expect(page.locator("#live-drawer")).to_be_visible()
        expect(page.locator("[data-datetime-toggle]").first).to_be_hidden()
        expect(page.locator("#live-datetime-date")).to_be_editable()
        for key in ("overview", "timeline", "results", "documents"):
            expect(page.locator(f"#tabs-default-{key}")).to_be_visible()
        expect(page.locator("#resource_grid-default a").first).to_be_visible()
        capture = _save(
            page, showcase.root / "no-js" / "behaviors.png", full_page=False
        )
        _record(
            showcase.report,
            assertion="without JavaScript: drawer in flow, date text inputs, every tab"
            " panel and every grid slot link reachable",
            capture=capture,
        )
    finally:
        context.close()
