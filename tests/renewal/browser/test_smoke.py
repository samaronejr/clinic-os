"""Smoke suite: open the real login and readiness surfaces as clinic_app.

``/readyz`` only reports ``ok`` when the serving connection resolves to
``current_user = clinic_app`` on the ``clinic_app`` schema with every leaf
migration applied, so a 200 is the runtime-role assertion.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from renewal.browser._page_wait import wait_for_js

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
    browser_report: dict[str, object],
) -> None:
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
            "surface": "readyz",
        }
    )


def test_login_surface_renders_the_real_csrf_form(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
) -> None:
    response = renewal_page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
    assert response is not None
    assert response.status == OK_STATUS
    renewal_page.wait_for_selector("input[name=csrfmiddlewaretoken]", state="attached")
    renewal_page.wait_for_selector("#id_username")
    renewal_page.wait_for_selector("#id_password")
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "login form renders with CSRF token",
            "capture": _capture(renewal_page, renewal_artifact_root, "login"),
            "surface": "auth/login",
        }
    )


def test_owner_login_establishes_a_session_and_enters_the_totp_flow(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    renewal_owner: dict[str, str],
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
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "owner login yields a session and the TOTP flow",
            "capture": _capture(renewal_page, renewal_artifact_root, "login-enrolled"),
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
        wait_for_js(csp_page, "() => window.finished", timeout=25)

    # The predicate turns true only after the deadline: the stopped watcher
    # neither reports it nor asks for another frame.
    assert csp_page.evaluate("window.frameRequests") == 1
    with csp_page.expect_console_message() as reported:
        csp_page.evaluate("window.finished = true")
        assert csp_page.evaluate(RUN_PENDING_FRAME_JS) == 1
        csp_page.evaluate("console.debug('after-late-frame')")
    assert reported.value.text == "after-late-frame"


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
