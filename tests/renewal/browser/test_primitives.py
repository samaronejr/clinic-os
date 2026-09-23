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
from django.template.loader import render_to_string
from django.utils.translation import gettext

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
VIEWPORTS = {
    "mobile-375": (375, 812),
    "tablet-768": (768, 1024),
    "desktop-1280": (1280, 800),
}
REFLOW_VIEWPORTS = {"reflow-320": (320, 640), "zoom-200-proxy-640": (640, 800)}
NAVY = "rgb(15, 45, 58)"
CYAN = "rgb(0, 229, 208)"
MIN_RING_PX = 2.0
MIN_CONTRAST = 4.5
MIN_LARGE_CONTRAST = 3.0
MAX_TAB_STOPS = 200

OVERFLOW_JS = """(root) => {
  const bad = [];
  for (const el of root.querySelectorAll('*')) {
    if (el.closest('.table-scroll') || el.tagName === 'INPUT') continue;
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
    within: rect.top >= -1 && rect.left >= -1 &&
      rect.bottom <= window.innerHeight + 1 && rect.right <= window.innerWidth + 1,
    primitive: (el.closest('[data-primitive]') || {}).dataset?.primitive || null,
    domIndex: Array.from(document.querySelectorAll('*')).indexOf(el),
  };
}"""

TABBABLE_COUNT_JS = """() => {
  const selector = 'a[href], button, input, select, textarea, [tabindex]';
  return Array.from(document.querySelectorAll(selector)).filter((el) => {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    if (el.tabIndex < 0) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
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
        assert set(captured) == set(PRIMITIVES)
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
