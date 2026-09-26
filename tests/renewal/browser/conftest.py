"""Fixtures bound to the renewal runner's private environment contract.

The runner exports ``CLINIC_RENEWAL_BASE_URL`` and
``CLINIC_RENEWAL_ARTIFACT_ROOT`` plus the browser executable and the seeded
owner credentials. Outside the runner these fixtures skip, and the runner's
junit gate rejects any skipped suite, so a bare pytest run can never fake a
green browser pass.
"""

from __future__ import annotations

import json
import os
import re
import weakref
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, cast

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    BrowserType,
    Locator,
    Page,
    sync_playwright,
)

from renewal.browser.engines import launch, selected_engine

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from playwright.sync_api import ConsoleMessage, Response

NAVIGATION_TIMEOUT_MS = 20_000
# Every engine reports a refused script/style/connection/eval as a console
# error naming the policy: Chromium and WebKit write "Content Security
# Policy", Firefox writes "Content-Security-Policy:".
CSP_CONSOLE_MARKER = re.compile(r"Content[ -]Security[ -]Policy")
CSP_CONSOLE_REPORT = "csp-console.json"
# Playwright's WebKit screenshotter appends a "body {}" <style> to every frame
# before each capture (screenshotter.ts inPagePrepareForScreenshots, because
# WebKit's shouldToggleStyleSheetToSyncAnimations() is true), and the page's
# style-src refuses it. No screenshot option avoids it, and the console entry
# is identical to an app inline-style refusal. So while a WebKit screenshot
# call runs, it may absorb at most one such refusal per frame; the allowance
# ends with the call, and any further refusal is still a violation. WebKit
# likewise refuses its own stylesheet when it renders a non-HTML response
# (JSON, text) as a document; one such refusal is absorbed per non-HTML
# main-frame navigation, until that page navigates again. Absorbed messages
# are hidden from the suites' own console listeners too.
WEBKIT_SCREENSHOT_REFUSAL = (
    "Refused to apply a stylesheet because its hash, its nonce, or "
    "'unsafe-inline' does not appear in the style-src directive of the "
    "Content Security Policy."
)


class _WebKitArtifacts:
    """WebKit CSP refusals Playwright or WebKit itself causes (see above).

    ``classify`` is the first console listener on every page (it is attached
    from the context's ``page`` event, before ``new_page`` returns), so the
    suites' own console listeners, filtered through ``Page.on``, never see a
    message it absorbed.
    """

    def __init__(self) -> None:
        self.screenshots: dict[int, int] = {}
        self.documents: dict[int, int] = {}
        self.absorbed: weakref.WeakSet[ConsoleMessage] = weakref.WeakSet()

    def track_document(self, response: Response) -> None:
        # Only navigations have a frame; service-worker requests raise on it.
        if not response.request.is_navigation_request():
            return
        frame = response.frame
        if frame.parent_frame is not None:
            return
        html = "html" in (response.headers.get("content-type") or "")
        webkit = _engine_name(frame.page) == "webkit"
        self.documents[id(frame.page)] = 1 if webkit and not html else 0

    def classify(self, message: ConsoleMessage) -> None:
        page = message.page
        if page is None or message.text != WEBKIT_SCREENSHOT_REFUSAL:
            return
        for budget, key in (
            (self.screenshots, id(page.context)),
            (self.documents, id(page)),
        ):
            if budget.get(key, 0):
                budget[key] -= 1
                self.absorbed.add(message)
                return

    def attach(self, page: Page) -> None:
        _PAGE_ON(page, "console", self.classify)

    def filtered(
        self, handler: Callable[[ConsoleMessage], object]
    ) -> Callable[[ConsoleMessage], None]:
        def deliver(message: ConsoleMessage) -> None:
            if message not in self.absorbed:
                handler(message)

        return deliver


def _engine_name(page: Page) -> str:
    browser = page.context.browser
    return browser.browser_type.name if browser is not None else ""


# The original listener registration, called with a runtime event name (the
# sync API's overloads only accept literal names).
_PAGE_ON = cast("Callable[[Page, str, Callable[..., object]], None]", Page.on)


def _csp_console_watch(
    violations: list[dict[str, str]],
    artifacts: _WebKitArtifacts,
) -> Callable[[BrowserContext | Page], None]:
    # Holding the contexts keeps identity checks exact for the session.
    watched: list[BrowserContext] = []

    def record(message: ConsoleMessage) -> None:
        if (
            CSP_CONSOLE_MARKER.search(message.text)
            and message not in artifacts.absorbed
        ):
            violations.append(
                {
                    "type": message.type,
                    "text": message.text,
                    "url": message.location.get("url", ""),
                }
            )

    def watch(target: BrowserContext | Page) -> None:
        context = target if isinstance(target, BrowserContext) else target.context
        if all(context is not known for known in watched):
            watched.append(context)
            for page in context.pages:
                artifacts.attach(page)
            context.on("page", artifacts.attach)
            context.on("response", artifacts.track_document)
            context.on("console", record)

    return watch


def _watching[**P, R](
    original: Callable[P, R],
    watch: Callable[[R], None],
) -> Callable[P, R]:
    @wraps(original)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        created = original(*args, **kwargs)
        watch(created)
        return created

    return wrapped


def _screenshot_window[T, **P](
    original: Callable[Concatenate[T, P], bytes],
    page_of: Callable[[T], Page],
    budget: dict[int, int],
) -> Callable[Concatenate[T, P], bytes]:
    @wraps(original)
    def wrapped(target: T, /, *args: P.args, **kwargs: P.kwargs) -> bytes:
        page = page_of(target)
        if _engine_name(page) != "webkit":
            return original(target, *args, **kwargs)
        key = id(page.context)
        budget[key] = len(page.frames)
        try:
            return original(target, *args, **kwargs)
        finally:
            budget.pop(key, None)

    return wrapped


@pytest.fixture(scope="session", autouse=True)
def csp_console_violations() -> Iterator[list[dict[str, str]]]:
    """Watch every browser context this session opens for CSP refusals.

    Suites launch their own browsers, so the public factories are wrapped
    once per session; the per-test guard below fails the test that caused a
    violation, and the runner artifact root receives the full log.
    """
    violations: list[dict[str, str]] = []
    artifacts = _WebKitArtifacts()
    watch = _csp_console_watch(violations, artifacts)

    def page_on(page: Page, event: str, handler: Callable[..., object]) -> None:
        if event == "console":
            handler = artifacts.filtered(handler)
        _PAGE_ON(page, event, handler)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Page, "on", page_on)
        patch.setattr(
            Page,
            "screenshot",
            _screenshot_window(
                Page.screenshot, lambda page: page, artifacts.screenshots
            ),
        )
        patch.setattr(
            Locator,
            "screenshot",
            _screenshot_window(
                Locator.screenshot, lambda loc: loc.page, artifacts.screenshots
            ),
        )
        patch.setattr(Browser, "new_context", _watching(Browser.new_context, watch))
        patch.setattr(Browser, "new_page", _watching(Browser.new_page, watch))
        patch.setattr(
            BrowserType,
            "launch_persistent_context",
            _watching(BrowserType.launch_persistent_context, watch),
        )
        yield violations
    root = os.environ.get("CLINIC_RENEWAL_ARTIFACT_ROOT", "")
    if root:
        destination = Path(root) / CSP_CONSOLE_REPORT
        destination.write_text(
            json.dumps({"violations": violations}, sort_keys=True, indent=2) + "\n"
        )
        destination.chmod(0o600)


@pytest.fixture(autouse=True)
def csp_console_guard(csp_console_violations: list[dict[str, str]]) -> Iterator[None]:
    """Fail the test during which any Content-Security-Policy refusal appeared."""
    start = len(csp_console_violations)
    yield
    caused = csp_console_violations[start:]
    assert not caused, f"Content-Security-Policy violations: {caused}"


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is only exported by the renewal runner")
    return value


@pytest.fixture(scope="session")
def renewal_base_url() -> str:
    """Return the supervised loopback base URL for this run."""
    return _required("CLINIC_RENEWAL_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def renewal_artifact_root() -> Path:
    """Return the runner-owned artifact directory for browser captures."""
    root = Path(_required("CLINIC_RENEWAL_ARTIFACT_ROOT"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def renewal_owner() -> dict[str, str]:
    """Return the seeded owner credentials for the login journey."""
    return {
        "username": _required("CLINIC_RENEWAL_USERNAME"),
        "password": _required("CLINIC_RENEWAL_PASSWORD"),
    }


@pytest.fixture(scope="session")
def renewal_engine() -> str:
    """Return the runner-selected engine (``CLINIC_BROWSER_ENGINE``)."""
    return selected_engine()


@pytest.fixture(scope="session")
def renewal_page(renewal_engine: str) -> Iterator[Page]:
    """Launch the runner-selected real browser engine and yield one page."""
    executable = _required("CLINIC_RENEWAL_BROWSER_EXECUTABLE")
    with sync_playwright() as driver:
        browser = launch(driver, renewal_engine, executable)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
        yield page
        context.close()
        browser.close()


@pytest.fixture(scope="session")
def browser_report(
    renewal_artifact_root: Path,
) -> Iterator[dict[str, object]]:
    """Collect per-test observations; a finalizer writes the report file."""
    report: dict[str, object] = {"schema_version": 1, "checks": []}
    yield report
    destination = renewal_artifact_root / "smoke-report.json"
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)
