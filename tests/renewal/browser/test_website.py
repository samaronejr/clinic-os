"""Todo 70: the public website and help center, as an anonymous visitor sees it.

Every page renders with axe 0 serious/critical, no horizontal overflow and
44px targets at desktop and on two phone profiles. Desktop and mobile captures
go to ``<artifact_root>/website/``. Each rendered claim must match the claims
registry, the pricing slot stays hidden, and the pages keep working with
forced colors, 200% zoom and JavaScript off. On the Chromium leg the
throttled-LCP budget of ``ops.testing.website_perf`` (Lighthouse mobile Slow
4G, 4x CPU, LCP < 2.5 s) holds for every page.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import pytest
from django.utils import translation
from ops.testing.website_perf import (
    LCP_BUDGET_MS,
    MOBILE_VIEWPORT,
    PAGES,
    measure_page,
    throttle,
    verdict,
)
from playwright.sync_api import expect

from renewal.browser import a11y_support
from renewal.browser._navigation import goto_settled
from renewal.browser._page_wait import wait_for_js
from renewal.browser.engines import (
    MOBILE_PROFILES,
    full_page_screenshot,
    mobile_context,
    selected_engine,
)
from website.test_claims import REGISTRY, load_claims

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import BrowserContext, Page, ViewportSize

OK: Final = 200
NOT_FOUND: Final = 404
MIN_TARGET_PX: Final = 44
ZOOM_200: Final[ViewportSize] = {"width": 640, "height": 450}
DESKTOP: Final[ViewportSize] = {"width": 1440, "height": 900}
NAMES: Final = {
    "/": "home",
    "/product/": "product",
    "/security/": "security",
    "/contact/": "contact",
    "/brand/": "brand",
    "/help/": "help",
    "/help/book-appointment/": "guide-book-appointment",
}
# Standalone targets only; links inside running text are exempt (WCAG 2.5.8).
TARGETS: Final = (
    ".site-nav a, .site-foot a, .button, .learn-link, .path-link, .guide-link,"
    " .track a, .aside a, .read-toc a, .crumbs a, .band-link, .downloads a"
)
OVERFLOW_JS: Final = "() => document.documentElement.scrollWidth - innerWidth"
# Buttons transition their colors; axe must sample the settled state, never a
# frame mid-transition (Firefox can start one while a phone context loads).
SETTLED_STYLES_JS: Final = "() => document.getAnimations().length === 0"
CLAIMS_JS: Final = """() => [...document.querySelectorAll('[data-claim]')].map(
  (element) => [element.dataset.claim, element.textContent.trim().replace(/\\s+/g, ' ')]
)"""
SMALL_TARGETS_JS: Final = """(selector) => [...document.querySelectorAll(selector)]
  .filter((element) => element.getClientRects().length > 0)
  .map((element) => [element.textContent.trim(), element.getBoundingClientRect()])
  .filter(([, box]) => box.height < 44 || box.width < 44)
  .map(([text, box]) => [text, Math.round(box.width), Math.round(box.height)])"""


def _folder(root: Path) -> Path:
    folder = root / "website"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _capture(page: Page, root: Path, name: str) -> list[str]:
    written = full_page_screenshot(page, _folder(root) / f"{name}.png")
    return [f"website/{path.name}" for path in written]


def _record(report: dict[str, object], entry: dict[str, object]) -> None:
    checks = report["checks"]
    assert isinstance(checks, list)
    checks.append({**entry, "engine": selected_engine()})


def _review(  # noqa: PLR0913 - one page state, its evidence and its report
    page: Page,
    base_url: str,
    root: Path,
    path: str,
    label: str,
    report: dict[str, object],
) -> None:
    response = goto_settled(page, base_url + path, wait_until="load")
    assert response is not None
    assert response.status == OK, path
    wait_for_js(page, SETTLED_STYLES_JS)
    assert page.evaluate(OVERFLOW_JS) <= 0, (path, label)
    assert page.evaluate(SMALL_TARGETS_JS, TARGETS) == [], (path, label)
    axe = a11y_support.check_page(
        page, base_url, root, f"website-{NAMES[path]}-{label}"
    )
    _record(
        report,
        {
            "assertion": "renders, no overflow, 44px targets, axe 0 serious/critical",
            "axe": axe,
            "capture": _capture(page, root, f"{NAMES[path]}-{label}"),
            "surface": path,
            "viewport": label,
        },
    )


@pytest.mark.parametrize("path", list(NAMES))
def test_each_page_on_desktop(
    path: str,
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(viewport=DESKTOP, locale="pt-BR")
    try:
        _review(
            context.new_page(),
            renewal_base_url,
            renewal_artifact_root,
            path,
            "desktop",
            browser_report,
        )
    finally:
        context.close()


@pytest.mark.parametrize("profile", sorted(MOBILE_PROFILES))
@pytest.mark.parametrize("path", list(NAMES))
def test_each_page_on_phone_profiles(  # noqa: PLR0913 - fixtures and scene
    path: str,
    profile: str,
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = mobile_context(browser, profile)
    try:
        page = context.new_page()
        _review(
            page,
            renewal_base_url,
            renewal_artifact_root,
            path,
            profile,
            browser_report,
        )
        assert page.evaluate("innerWidth") == MOBILE_PROFILES[profile].width
    finally:
        context.close()


def test_rendered_claims_match_the_registry_word_for_word(
    renewal_page: Page, renewal_base_url: str, browser_report: dict[str, object]
) -> None:
    claims = {
        claim_id: claim
        for claim_id, claim in load_claims(REGISTRY).items()
        if claim.surface == "pages"
    }
    seen: set[str] = set()
    with translation.override("pt-br"):
        expected = {
            claim.id: " ".join(translation.gettext(claim.claim).split())
            for claim in claims.values()
        }
    for path in ("/", "/product/", "/security/", "/brand/", "/help/"):
        goto_settled(renewal_page, renewal_base_url + path, wait_until="load")
        for claim_id, text in renewal_page.evaluate(CLAIMS_JS):
            assert text == expected[claim_id], (path, claim_id)
            seen.add(claim_id)
    assert seen == set(claims)
    _record(
        browser_report,
        {
            "assertion": "every rendered claim is registered with evidence",
            "claims": len(seen),
            "surface": "claims",
        },
    )


def test_keyboard_starts_at_the_skip_link_and_reaches_the_navigation(
    renewal_page: Page, renewal_base_url: str
) -> None:
    goto_settled(renewal_page, renewal_base_url + "/", wait_until="load")
    renewal_page.keyboard.press("Tab")
    skip = renewal_page.locator(".skip-link")
    expect(skip).to_be_focused()
    expect(skip).to_be_in_viewport()
    renewal_page.keyboard.press("Tab")
    expect(renewal_page.locator(".site-brand")).to_be_focused()
    renewal_page.keyboard.press("Tab")
    product = renewal_page.locator(".site-nav-link").first
    expect(product).to_be_focused()
    assert product.evaluate("e => getComputedStyle(e).outlineStyle") != "none"


def test_pricing_slot_stays_hidden_and_unlinked(
    renewal_page: Page, renewal_base_url: str
) -> None:
    response = goto_settled(
        renewal_page, renewal_base_url + "/pricing/", wait_until="load"
    )
    assert response is not None
    assert response.status == NOT_FOUND
    for path in NAMES:
        goto_settled(renewal_page, renewal_base_url + path, wait_until="load")
        assert renewal_page.locator("a[href*='/pricing']").count() == 0, path


def test_forced_colors_zoom_and_javascript_off_keep_the_content(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    scenes: dict[str, BrowserContext] = {
        "forced-colors": browser.new_context(
            viewport=DESKTOP, locale="pt-BR", forced_colors="active"
        ),
        "zoom-200": browser.new_context(
            viewport=ZOOM_200,
            device_scale_factor=2,
            locale="pt-BR",
            reduced_motion="reduce",
        ),
        "no-javascript": browser.new_context(
            viewport=DESKTOP, locale="pt-BR", java_script_enabled=False
        ),
    }
    try:
        for scene, context in scenes.items():
            page = context.new_page()
            goto_settled(page, renewal_base_url + "/", wait_until="load")
            expect(page.locator("h1")).to_be_visible()
            if scene == "forced-colors":
                expect(page.locator(".site-brand-name")).to_be_visible()
            if scene != "no-javascript":
                assert page.evaluate(OVERFLOW_JS) <= 0, scene
            _record(
                browser_report,
                {
                    "assertion": f"home keeps its content: {scene}",
                    "capture": _capture(page, renewal_artifact_root, f"home-{scene}"),
                    "surface": "/",
                },
            )
    finally:
        for context in scenes.values():
            context.close()


# DevTools network and CPU throttling exist only on Chromium, so the budget
# scene is defined on the Chromium leg alone (never an empty or skipped
# parametrization); every other scene runs on every engine.
if selected_engine() == "chromium":

    def test_every_page_meets_the_throttled_lcp_budget(
        renewal_page: Page,
        renewal_base_url: str,
        renewal_artifact_root: Path,
        browser_report: dict[str, object],
    ) -> None:
        browser = renewal_page.context.browser
        assert browser is not None
        measurements = []
        for path in PAGES:
            context = browser.new_context(
                viewport=MOBILE_VIEWPORT, is_mobile=True, has_touch=True
            )
            try:
                page = context.new_page()
                throttle(page)
                measurements.append(measure_page(page, renewal_base_url, path))
            finally:
                context.close()
        report = verdict(measurements)
        destination = _folder(renewal_artifact_root) / "perf-budget.json"
        destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        destination.chmod(0o600)
        slow = [(m.path, m.lcp_ms) for m in measurements if not m.within_budget]
        assert slow == [], (LCP_BUDGET_MS, slow)
        _record(
            browser_report,
            {
                "assertion": f"LCP < {LCP_BUDGET_MS:.0f} ms on Slow 4G with 4x CPU",
                "lcp_ms": {m.path: round(m.lcp_ms) for m in measurements},
                "report": "website/perf-budget.json",
                "surface": "perf",
            },
        )
