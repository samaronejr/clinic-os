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
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Browser, BrowserContext, BrowserType, sync_playwright

from renewal.browser.engines import launch, selected_engine

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from playwright.sync_api import ConsoleMessage, Page

NAVIGATION_TIMEOUT_MS = 20_000
# Chromium reports every refused script/style/connection (enforced or
# "[Report Only]") as a console entry naming the Content Security Policy.
CSP_CONSOLE_MARKER = "Content Security Policy"
CSP_CONSOLE_REPORT = "csp-console.json"


def _csp_console_watch(
    violations: list[dict[str, str]],
) -> Callable[[BrowserContext | Page], None]:
    # Holding the contexts keeps identity checks exact for the session.
    watched: list[BrowserContext] = []

    def record(message: ConsoleMessage) -> None:
        if CSP_CONSOLE_MARKER in message.text:
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


@pytest.fixture(scope="session", autouse=True)
def csp_console_violations() -> Iterator[list[dict[str, str]]]:
    """Watch every browser context this session opens for CSP refusals.

    Suites launch their own browsers, so the public factories are wrapped
    once per session; the per-test guard below fails the test that caused a
    violation, and the runner artifact root receives the full log.
    """
    violations: list[dict[str, str]] = []
    watch = _csp_console_watch(violations)
    with pytest.MonkeyPatch.context() as patch:
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
