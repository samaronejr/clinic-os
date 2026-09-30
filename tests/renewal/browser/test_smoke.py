"""Smoke suite: open the real login and readiness surfaces as clinic_app.

``/readyz`` only reports ``ok`` when the serving connection resolves to
``current_user = clinic_app`` on the ``clinic_app`` schema with every leaf
migration applied, so a 200 is the runtime-role assertion.

The suite is engine-portable (``CLINIC_BROWSER_ENGINE``): every HTML page
state runs the shared axe check, and the login surface is driven by touch
in the iPhone 15 and Pixel 8 emulation profiles.
"""

from __future__ import annotations

import http.server
import json
import math
import threading
import time
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import ViewportSize

from renewal.browser._navigation import (
    click_to_navigate,
    goto_settled,
    settle_service_worker,
)
from renewal.browser._page_wait import (
    EvaluateTimeoutError,
    await_autofocus,
    click_when_hittable,
    evaluate_all_js,
    evaluate_js,
    wait_for_js,
)
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import (
    MAX_CAPTURE_DEVICE_PX,
    MOBILE_PROFILES,
    PAGE_EXTENT_JS,
    element_box,
    full_page_screenshot,
    mobile_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Page

OK_STATUS = 200
FOUND_STATUS = 302


def _capture(page: Page, artifact_root: Path, label: str) -> str:
    destination = artifact_root / f"{label}.png"
    return ", ".join(path.name for path in full_page_screenshot(page, destination))


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
    response = goto_settled(
        renewal_page, f"{renewal_base_url}/readyz", wait_until="load"
    )
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
    response = goto_settled(
        renewal_page, f"{renewal_base_url}/auth/login/", wait_until="load"
    )
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
        response = goto_settled(
            page, f"{renewal_base_url}/auth/login/", wait_until="load"
        )
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
    goto_settled(renewal_page, f"{renewal_base_url}/auth/login/", wait_until="load")
    await_autofocus(renewal_page.locator("#id_username"))
    renewal_page.fill("#id_username", renewal_owner["username"])
    renewal_page.fill("#id_password", renewal_owner["password"])
    click_to_navigate(renewal_page.locator("button[type=submit]"), wait_until="load")
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
    response = goto_settled(page, f"{renewal_base_url}/auth/login/", wait_until="load")
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


# Self-tests of the bounded one-shot evaluate the capture helpers use after
# each screenshot (fix-a8: a wedged page held video-recovery until the
# runner's 900 s bound).
def test_evaluate_js_answers_once_like_evaluate(csp_page: Page) -> None:
    # A falsy answer comes straight back; it is never waited on.
    assert evaluate_js(csp_page, "document.documentElement.scrollWidth < 0") is False
    assert evaluate_js(csp_page, "(n) => n + 1", 1) == 2
    assert evaluate_js(csp_page.locator("form"), "(form) => form.method") == "post"
    fields = csp_page.locator("form input").count()
    assert fields > 0
    assert evaluate_all_js(csp_page.locator("form input"), "(all) => all.length") == (
        fields
    )

    with pytest.raises(PlaywrightError, match="sentinel-error"):
        evaluate_js(csp_page, "() => { throw new Error('sentinel-error'); }")


def test_evaluate_js_fails_at_its_deadline_while_the_page_is_busy(
    csp_page: Page,
) -> None:
    started = time.monotonic()
    with pytest.raises(EvaluateTimeoutError, match="Timeout 25ms exceeded"):
        evaluate_js(csp_page, BUSY_THEN_TRUE_JS, timeout=BUSY_TIMEOUT_MS)
    elapsed_ms = (time.monotonic() - started) * 1000

    assert elapsed_ms < BUSY_TIMEOUT_MS + DEADLINE_SLACK_MS
    # The page answers the next evaluation once it is free again.
    assert evaluate_js(csp_page, "document.readyState") == "complete"


def test_evaluate_js_honors_the_page_default_timeout(csp_page: Page) -> None:
    csp_page.set_default_timeout(40)

    with pytest.raises(EvaluateTimeoutError, match="Timeout 40ms exceeded"):
        evaluate_js(csp_page, BUSY_THEN_TRUE_JS)
    with pytest.raises(EvaluateTimeoutError, match="Timeout 40ms exceeded"):
        evaluate_all_js(csp_page.locator("form"), BUSY_THEN_TRUE_JS)


# 40000 CSS px at devicePixelRatio 1: past the 32767 device-pixel limit that
# failed hosted primitives@chromium at 375px.
TALL_PAGE_PX = 40_000
NARROW_VIEWPORT: ViewportSize = {"width": 375, "height": 667}


def test_capture_splits_a_narrow_page_taller_than_the_screenshot_limit(
    csp_page: Page, renewal_artifact_root: Path
) -> None:
    csp_page.set_viewport_size(NARROW_VIEWPORT)
    csp_page.evaluate(f"document.body.style.minHeight = '{TALL_PAGE_PX}px'")
    width, height, ratio = csp_page.evaluate(PAGE_EXTENT_JS)
    assert (width, ratio) == (NARROW_VIEWPORT["width"], 1)
    assert height > MAX_CAPTURE_DEVICE_PX

    names = _capture(csp_page, renewal_artifact_root, "tall-375").split(", ")

    count = math.ceil(height / MAX_CAPTURE_DEVICE_PX)
    assert names == [f"tall-375-part{index}.png" for index in range(1, count + 1)]
    assert not (renewal_artifact_root / "tall-375.png").exists()
    sizes = []
    for name in names:
        part = renewal_artifact_root / name
        assert part.stat().st_mode & 0o777 == 0o600
        with Image.open(part) as image:
            sizes.append(image.size)
    # Full-width sections, each within the limit, that cover the whole page.
    assert {section_width for section_width, _ in sizes} == {width}
    assert max(section_height for _, section_height in sizes) <= MAX_CAPTURE_DEVICE_PX
    assert sum(section_height for _, section_height in sizes) == height


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


# click_when_hittable: a far-below target is pressed once, after scrolling; a
# covered or moving target is never pressed and the helper's own wait fails at
# its deadline (never a blind click).
PRESS_PROBE_JS = """(mode) => {
  const spacer = document.createElement('div');
  spacer.style.height = '3000px';
  const button = document.createElement('button');
  button.type = 'button';
  button.id = 'press-probe';
  button.style.cssText = 'position: relative; width: 120px; height: 44px;';
  const label = document.createElement('span');
  label.textContent = 'Pressionar';
  button.append(label);
  window.pressProbeClicks = 0;
  window.pressProbeDowns = 0;
  button.addEventListener('mousedown', () => { window.pressProbeDowns += 1; });
  button.addEventListener('click', () => { window.pressProbeClicks += 1; });
  document.body.append(spacer, button);
  if (mode === 'covered') {
    const cover = document.createElement('div');
    cover.id = 'press-probe-cover';
    cover.style.cssText = 'position: fixed; inset: 0; z-index: 10;';
    document.body.append(cover);
  }
  if (mode === 'moving') {
    // Still while Playwright scrolls it into view, then it moves on every
    // frame: only the helper's own stability check can refuse it.
    let shift = 0;
    const move = () => {
      shift = (shift + 1) % 7;
      button.style.top = shift + 'px';
      requestAnimationFrame(move);
    };
    addEventListener('scroll', () => requestAnimationFrame(move), {once: true});
  }
}"""
PRESS_PROBE_TIMEOUT_MS = 400
REFUSED_BY = {
    "covered": "wait_for_js: Timeout",
    "moving": "ElementHandle.wait_for_element_state: Timeout",
}


def test_click_when_hittable_scrolls_far_targets_and_presses_once(
    csp_page: Page,
) -> None:
    csp_page.evaluate(PRESS_PROBE_JS, "still")
    assert csp_page.evaluate("scrollY") == 0

    click_when_hittable(csp_page.locator("#press-probe"))

    assert csp_page.evaluate("[pressProbeDowns, pressProbeClicks]") == [1, 1]
    assert csp_page.evaluate("scrollY") > 0


@pytest.mark.parametrize("mode", ["covered", "moving"])
def test_click_when_hittable_fails_loudly_without_pressing(
    csp_page: Page, mode: str
) -> None:
    csp_page.evaluate(PRESS_PROBE_JS, mode)

    with pytest.raises(
        PlaywrightTimeoutError, match=f"Timeout {PRESS_PROBE_TIMEOUT_MS}ms exceeded"
    ) as refused:
        click_when_hittable(
            csp_page.locator("#press-probe"), timeout=PRESS_PROBE_TIMEOUT_MS
        )
    # The helper's own precondition refused it; no click was ever attempted.
    assert str(refused.value).startswith(REFUSED_BY[mode]), refused.value

    assert csp_page.evaluate("[pressProbeDowns, pressProbeClicks]") == [0, 0]


# A registration whose activation is held on the origin's /gate: the app
# worker's shape (skipWaiting at install, clients.claim at activate, a fetch
# handler that leaves navigations to the network), with the claim deferred
# until the test opens the gate. Without a fetch handler no engine routes a
# navigation through the worker, so nothing would be held. Pages carry the
# app's data-service-worker marker and register on load, as the shell does.
HELD_WORKER = b"""self.addEventListener('install', (event) =>
  event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) =>
  event.waitUntil(fetch('/gate').then(() => self.clients.claim())));
self.addEventListener('fetch', () => {});"""
HELD_SHELL = b"""window.pressProbeClicks = 0;
addEventListener('click', () => { window.pressProbeClicks += 1; }, {capture: true});
addEventListener('load', () =>
  navigator.serviceWorker.register('/sw.js', {scope: '/'}));"""
HELD_PAGE = (
    b'<!doctype html><html lang="pt-BR" data-service-worker="/sw.js">'
    b'<title>held</title><script src="/shell.js"></script>'
    b'<form method="post" action="/submit">'
    b'<button id="press" name="action" value="open">Abrir</button></form></html>'
)
HELD_PROBE_TIMEOUT_MS = 1_000
# Bounds the server thread's wait for a gate the test never opens (a failure).
HELD_GATE_BOUND_S = 60


class _HeldOrigin(http.server.ThreadingHTTPServer):
    """A loopback origin that records requests and holds /gate until opened."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HeldHandler)
        self.requests: list[str] = []
        self.activating = threading.Event()
        self.gate = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def posts(self) -> list[str]:
        return [line for line in self.requests if line.startswith("POST ")]


class _HeldHandler(http.server.BaseHTTPRequestHandler):
    server: _HeldOrigin

    def _send(self, status: int, body: bytes, kind: str, *headers: str) -> None:
        self.send_response(status)
        for header in headers:
            name, value = header.split(": ", 1)
            self.send_header(name, value)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.server.requests.append(f"GET {self.path}")
        if self.path == "/gate":
            self.server.activating.set()
            self.server.gate.wait(HELD_GATE_BOUND_S)
            self._send(200, b"open", "text/plain")
        elif self.path == "/sw.js":
            self._send(200, HELD_WORKER, "text/javascript")
        elif self.path == "/shell.js":
            self._send(200, HELD_SHELL, "text/javascript")
        else:
            self._send(200, HELD_PAGE, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append(f"POST {self.path}")
        self._send(302, b"", "text/html", "Location: /done")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        del format, args


@pytest.fixture
def held_origin() -> Iterator[_HeldOrigin]:
    origin = _HeldOrigin()
    serving = threading.Thread(target=origin.serve_forever, daemon=True)
    serving.start()
    try:
        yield origin
    finally:
        origin.gate.set()
        origin.shutdown()
        serving.join()
        origin.server_close()


def _unsafe_press(page: Page) -> None:
    """The pre-fix ``press``: no worker precondition before the click.

    Kept only to reproduce the hosted hang.
    """
    with page.expect_navigation(timeout=HELD_PROBE_TIMEOUT_MS):
        page.locator("#press").click(timeout=HELD_PROBE_TIMEOUT_MS)


def _unsafe_goto(page: Page, url: str) -> None:
    """A ``goto`` with no worker precondition; kept only to reproduce the hold."""
    page.goto(url, timeout=HELD_PROBE_TIMEOUT_MS)


def test_navigation_helper_waits_out_a_held_worker_activation(
    renewal_page: Page, held_origin: _HeldOrigin
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context()
    try:
        page = context.new_page()
        # The origin's own page registers the gated worker (it cannot settle).
        page.goto(held_origin.url, wait_until="load")
        assert held_origin.activating.wait(HELD_GATE_BOUND_S)

        # The helpers refuse to press or navigate while the worker activates:
        # their precondition times out, no click is sent and no goto starts.
        with pytest.raises(EvaluateTimeoutError):
            click_to_navigate(page.locator("#press"), timeout=HELD_PROBE_TIMEOUT_MS)
        assert evaluate_js(page, "window.pressProbeClicks") == 0
        with pytest.raises(EvaluateTimeoutError):
            goto_settled(
                page, f"{held_origin.url}second", timeout=HELD_PROBE_TIMEOUT_MS
            )
        assert "GET /second" not in held_origin.requests
        assert page.url == held_origin.url

        # An unsettled goto into the scope is held too (gate review B1): it
        # times out in Page.goto.
        second = context.new_page()
        with pytest.raises(PlaywrightTimeoutError, match=r"Page\.goto"):
            _unsafe_goto(second, f"{held_origin.url}second")
        second.close()

        # The pre-fix shape reproduces hosted retention@firefox 36349167980:
        # the click is dispatched, the POST is held and never sent, and the
        # click hangs in its own wait for the navigation it scheduled.
        with pytest.raises(PlaywrightTimeoutError) as held:
            _unsafe_press(page)
        assert "waiting for scheduled navigations to finish" in str(held.value)
        assert held_origin.posts() == []

        # Activation ends: the held POST goes out, exactly once.
        held_origin.gate.set()
        page.wait_for_url("**/done")
        assert held_origin.posts() == ["POST /submit"]

        # A settled worker: the helper presses once and awaits the document.
        response = click_to_navigate(page.locator("#press"), url="**/done")
        assert response is not None
        assert response.status == OK_STATUS
        assert held_origin.posts() == ["POST /submit", "POST /submit"]
    finally:
        context.close()


@pytest.mark.parametrize(
    ("mode", "settled"),
    [
        ("js-off", "not applicable"),
        ("blocked", "not applicable"),
        ("default", "unregistered"),
    ],
)
def test_settle_reads_the_context_options_it_depends_on(
    renewal_page: Page, renewal_base_url: str, mode: str, settled: str
) -> None:
    # settle_service_worker reads Playwright 1.61's private context options
    # (_impl_obj._options): an upgrade that moves them fails here, not as a
    # hang in a JS-disabled page.
    browser = renewal_page.context.browser
    assert browser is not None
    if mode == "js-off":
        context = browser.new_context(java_script_enabled=False)
    elif mode == "blocked":
        context = browser.new_context(service_workers="block")
    else:
        context = browser.new_context()
    try:
        page = context.new_page()
        goto_settled(page, f"{renewal_base_url}/auth/login/")
        assert settle_service_worker(page) == settled
    finally:
        context.close()
