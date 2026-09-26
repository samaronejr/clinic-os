"""Smoke suite: open the real login and readiness surfaces as clinic_app.

``/readyz`` only reports ``ok`` when the serving connection resolves to
``current_user = clinic_app`` on the ``clinic_app`` schema with every leaf
migration applied, so a 200 is the runtime-role assertion.

The suite is engine-portable (``CLINIC_BROWSER_ENGINE``): every HTML page
state runs the shared axe check, and the login surface is driven by touch
in the iPhone 15 and Pixel 8 emulation profiles.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from renewal.browser._page_wait import wait_for_js
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import MOBILE_PROFILES, element_box, mobile_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Page

OK_STATUS = 200
FOUND_STATUS = 302


def _capture(page: Page, artifact_root: Path, label: str) -> str:
    destination = artifact_root / f"{label}.png"
    destination.write_bytes(page.screenshot(full_page=True))
    destination.chmod(0o600)
    return destination.name


def test_readiness_reports_the_migrated_app_role(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    renewal_engine: str,
    browser_report: dict[str, object],
) -> None:
    # The launched browser really is the engine the runner selected.
    browser = renewal_page.context.browser
    assert browser is not None
    assert browser.browser_type.name == renewal_engine
    response = renewal_page.goto(f"{renewal_base_url}/readyz", wait_until="load")
    assert response is not None
    assert response.status == OK_STATUS
    body = json.loads(response.body())
    assert body == {"status": "ok"}
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "readyz ok implies current_user=clinic_app",
            "capture": _capture(renewal_page, renewal_artifact_root, "readyz"),
            "engine": renewal_engine,
            "surface": "readyz",
        }
    )


def test_login_surface_renders_the_real_csrf_form(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    renewal_engine: str,
    browser_report: dict[str, object],
) -> None:
    response = renewal_page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
    assert response is not None
    assert response.status == OK_STATUS
    renewal_page.wait_for_selector("input[name=csrfmiddlewaretoken]", state="attached")
    renewal_page.wait_for_selector("#id_username")
    renewal_page.wait_for_selector("#id_password")
    axe = check_page(renewal_page, renewal_base_url, renewal_artifact_root, "login")
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "login form renders with CSRF token; axe 0 serious/critical",
            "axe": axe,
            "capture": _capture(renewal_page, renewal_artifact_root, "login"),
            "engine": renewal_engine,
            "surface": "auth/login",
        }
    )


@pytest.mark.parametrize("profile", sorted(MOBILE_PROFILES))
def test_login_surface_by_touch_on_mobile_profiles(  # noqa: PLR0913 - fixtures
    profile: str,
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    renewal_engine: str,
    browser_report: dict[str, object],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = mobile_context(browser, profile)
    try:
        page = context.new_page()
        response = page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
        assert response is not None
        assert response.status == OK_STATUS
        device = MOBILE_PROFILES[profile]
        assert page.evaluate("innerWidth") == device.width
        # navigator.maxTouchPoints is only emulated by Chromium; the coarse
        # pointer media query and a real touchstart hold on every engine.
        assert page.evaluate("matchMedia('(pointer: coarse)').matches") is True
        assert page.evaluate("document.scrollingElement.scrollWidth") <= device.width
        page.evaluate(
            "window.__touched = false; document.addEventListener('touchstart',"
            " () => { window.__touched = true; }, {once: true})"
        )
        page.tap("#id_username")
        assert page.evaluate("window.__touched") is True
        assert page.evaluate("document.activeElement.id") == "id_username"
        axe = check_page(
            page, renewal_base_url, renewal_artifact_root, f"login-{profile}"
        )
        checks = browser_report["checks"]
        assert isinstance(checks, list)
        checks.append(
            {
                "assertion": f"{device.label}: touch focus, no overflow, axe clean",
                "axe": axe,
                "capture": _capture(page, renewal_artifact_root, f"login-{profile}"),
                "engine": renewal_engine,
                "surface": "auth/login",
            }
        )
    finally:
        context.close()


def test_owner_login_establishes_a_session_and_enters_the_totp_flow(  # noqa: PLR0913 - fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    renewal_owner: dict[str, str],
    renewal_engine: str,
    browser_report: dict[str, object],
) -> None:
    renewal_page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
    renewal_page.fill("#id_username", renewal_owner["username"])
    renewal_page.fill("#id_password", renewal_owner["password"])
    with renewal_page.expect_navigation(wait_until="load"):
        renewal_page.click("button[type=submit]")
    assert any(
        cookie["name"] == "sessionid" for cookie in renewal_page.context.cookies()
    )
    # The owner is a privileged role: the protected default target must hand
    # the session to the TOTP enrollment flow, never render it directly.
    renewal_page.wait_for_url("**/auth/enroll/**")
    axe = check_page(renewal_page, renewal_base_url, renewal_artifact_root, "enroll")
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "owner login yields a session and the TOTP flow",
            "axe": axe,
            "capture": _capture(renewal_page, renewal_artifact_root, "login-enrolled"),
            "engine": renewal_engine,
            "surface": "auth/enroll",
        }
    )


# Self-tests of the CSP-safe wait every suite uses instead of
# ``Page.wait_for_function`` (which evals in the page and is refused by the
# strict policy). They run on the real, CSP-protected login page.
# The page captures animation-frame callbacks instead of running them, so the
# test decides exactly when the watcher polls again.
CAPTURE_FRAMES_JS = """() => {
  window.finished = false;
  window.checks = 0;
  window.frameRequests = 0;
  window.pendingFrame = null;
  window.requestAnimationFrame = (callback) => {
    window.frameRequests += 1;
    window.pendingFrame = callback;
    return window.frameRequests;
  };
}"""
RUN_PENDING_FRAME_JS = """() => {
  const callback = window.pendingFrame;
  window.pendingFrame = null;
  callback(performance.now());
  return window.frameRequests;
}"""
# The gate reviewer's probe: every frame arrives 250 ms late, then the
# predicate is already true.
LATE_FRAMES_JS = """() => {
  window.finished = false;
  window.requestAnimationFrame = (callback) => setTimeout(() => {
    window.finished = true;
    callback(performance.now());
  }, 250);
}"""


@pytest.fixture
def csp_page(renewal_page: Page, renewal_base_url: str) -> Iterator[Page]:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context()
    page = context.new_page()
    response = page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
    assert response is not None
    assert "script-src 'self'" in response.headers["content-security-policy"]
    yield page
    context.close()


def test_wait_for_js_fails_at_its_deadline_before_a_late_truth(
    csp_page: Page,
) -> None:
    csp_page.evaluate(LATE_FRAMES_JS)

    with pytest.raises(PlaywrightTimeoutError, match="Timeout 25ms exceeded"):
        wait_for_js(csp_page, "() => window.finished", timeout=25)


def test_wait_for_js_deadline_does_not_depend_on_animation_frames(
    csp_page: Page,
) -> None:
    csp_page.evaluate(CAPTURE_FRAMES_JS)

    with pytest.raises(PlaywrightTimeoutError, match="Timeout 25ms exceeded"):
        wait_for_js(
            csp_page,
            "() => { window.checks += 1; return window.finished; }",
            timeout=25,
        )

    # The predicate turns true only after the deadline: the stopped watcher
    # does not evaluate it again, report it, or ask for another frame.
    assert csp_page.evaluate("[window.checks, window.frameRequests]") == [1, 1]
    with csp_page.expect_console_message() as reported:
        csp_page.evaluate("window.finished = true")
        assert csp_page.evaluate(RUN_PENDING_FRAME_JS) == 1
        csp_page.evaluate("console.debug('after-late-frame')")
    assert reported.value.text == "after-late-frame"
    assert csp_page.evaluate("window.checks") == 1


# Gate review fix1-B1: a predicate that keeps the renderer busy for a second
# and then returns true. Time is the behavior under test: the 25 ms deadline
# must fire from the driver while the page is still busy.
BUSY_THEN_TRUE_JS = """() => {
  const end = performance.now() + 1000;
  while (performance.now() < end) {}
  return true;
}"""
BUSY_TIMEOUT_MS = 25
# Generous scheduling slack, still far below the 1000 ms the page stays busy.
DEADLINE_SLACK_MS = 250


def test_wait_for_js_deadline_is_not_delayed_by_a_busy_predicate(
    csp_page: Page,
) -> None:
    started = time.monotonic()
    with pytest.raises(PlaywrightTimeoutError, match="Timeout 25ms exceeded"):
        wait_for_js(csp_page, BUSY_THEN_TRUE_JS, timeout=BUSY_TIMEOUT_MS)
    elapsed_ms = (time.monotonic() - started) * 1000

    assert elapsed_ms < BUSY_TIMEOUT_MS + DEADLINE_SLACK_MS
    # This evaluate runs only once the busy predicate has returned true; a
    # late success would already be in the console ahead of the marker.
    with csp_page.expect_console_message() as first:
        csp_page.evaluate("console.debug('after-busy-predicate')")
    assert first.value.text == "after-busy-predicate"


def test_wait_for_js_honors_the_page_default_timeout(csp_page: Page) -> None:
    csp_page.set_default_timeout(40)

    with pytest.raises(PlaywrightTimeoutError, match="Timeout 40ms exceeded"):
        wait_for_js(csp_page, "false")


def test_wait_for_js_resolves_values_and_surfaces_predicate_errors(
    csp_page: Page,
) -> None:
    assert wait_for_js(csp_page, "document.readyState === 'complete'").json_value()
    assert wait_for_js(csp_page, "(n) => n + 1", arg=1).json_value() == 2

    with pytest.raises(PlaywrightError, match="predicate threw: sentinel-error"):
        wait_for_js(csp_page, "() => { throw new Error('sentinel-error'); }")


INJECT_INLINE_SCRIPT_JS = """() => {
  const script = document.createElement('script');
  script.textContent = 'window.inlineScriptRan = true';
  document.head.append(script);
}"""


def test_csp_console_hook_catches_a_refused_inline_script(
    csp_page: Page,
    csp_console_violations: list[dict[str, str]],
) -> None:
    # Each engine words the refusal differently; the session hook must see
    # this one on Chromium, Firefox and WebKit alike.
    before = len(csp_console_violations)
    with csp_page.expect_console_message(lambda m: m.type == "error") as refused:
        csp_page.evaluate(INJECT_INLINE_SCRIPT_JS)
    assert csp_page.evaluate("window.inlineScriptRan === undefined")
    assert [v["text"] for v in csp_console_violations[before:]] == [refused.value.text]
    # The deliberate refusal is this test's subject, not a defect.
    del csp_console_violations[before:]


INJECT_INLINE_STYLE_JS = """() => {
  const style = document.createElement('style');
  style.textContent = 'body { outline: 1px solid red; }';
  document.head.append(style);
}"""


def test_csp_console_hook_tolerates_only_playwright_screenshot_style(
    csp_page: Page,
    csp_console_violations: list[dict[str, str]],
) -> None:
    # WebKit's screenshotter injects its own style; that alone is not a
    # violation, but an app inline style right after it still is.
    before = len(csp_console_violations)
    csp_page.screenshot()
    csp_page.locator("form").screenshot()
    assert csp_console_violations[before:] == []
    with csp_page.expect_console_message(lambda m: m.type == "error") as refused:
        csp_page.evaluate(INJECT_INLINE_STYLE_JS)
    assert [v["text"] for v in csp_console_violations[before:]] == [refused.value.text]
    del csp_console_violations[before:]


# Self-test of engines.element_box (Element size). The button's top sits one
# Gecko app unit (1/60 px) past 1000px, so its box crosses y=1024; there
# Playwright's own Firefox bounding_box() measures it 43.99994px tall.
STRADDLING_BUTTON_JS = """() => {
  const button = document.createElement('button');
  button.id = 'element-box-probe';
  button.textContent = 'x';
  Object.assign(button.style, {
    position: 'absolute', left: '0px', top: (60001 / 60) + 'px',
    width: '44px', height: '44px', margin: '0px', padding: '0px',
    border: '0px', boxSizing: 'border-box',
  });
  document.body.append(button);
}"""


def test_element_box_measures_a_straddling_target_exactly(csp_page: Page) -> None:
    csp_page.evaluate(STRADDLING_BUTTON_JS)
    box = element_box(csp_page.locator("#element-box-probe"))
    assert (box["width"], box["height"]) == (44, 44), box
