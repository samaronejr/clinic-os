"""Shared live-browser driving primitives every clinic suite reuses.

Suites differ only in the journey they walk. Sign-in, viewport sweeping, the
accessibility audit, console capture, screen identity, and POST-only form
assertions are identical, so they live here and cannot drift between suites.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from ops.testing.browser_visual_contract import (
    AUDIT_SCRIPT,
    VIEWPORTS,
    VisualContractError,
    require_clean_console,
    require_no_blocking_violations,
    require_post_only_forms,
)

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Error, Page

CONFIG_KEYS: Final = frozenset({"base_url", "clinic_id", "password", "username"})
CONSOLE_LEVELS: Final = frozenset({"error", "warning"})
SESSION_TIMEOUT_SECONDS: Final = 15
SESSION_POLL_SECONDS: Final = 0.1
FORM_SHAPE_LENGTH: Final = 2


class BrowserDriverError(RuntimeError):
    """Reject a malformed audit or form-shape response from the live page."""

    def __init__(self) -> None:
        """Expose one stable non-identifying driver failure message."""
        super().__init__("browser suite driver response rejected")


def record_console(message: ConsoleMessage, console: list[str]) -> None:
    """Record one error or warning the live page emitted to its console."""
    if message.type in CONSOLE_LEVELS:
        console.append(f"{message.type}:{message.text}")


def record_error(error: Error, console: list[str]) -> None:
    """Record one uncaught page error as a console failure."""
    console.append(f"pageerror:{error.message}")


def evaluate_audit(page: Page) -> list[dict[str, object]]:
    """Run the shared in-page WCAG audit and normalize its findings."""
    raw: object = page.evaluate(AUDIT_SCRIPT)
    if not isinstance(raw, list):
        raise BrowserDriverError
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BrowserDriverError
        result.append({str(key): value for key, value in item.items()})
    return result


def resize(page: Page, viewport: dict[str, object]) -> None:
    """Apply one responsive viewport and its browser zoom factor."""
    page.set_viewport_size(
        {"width": int(str(viewport["width"])), "height": int(str(viewport["height"]))}
    )
    page.evaluate(
        "(factor) => { document.documentElement.style.zoom = String(factor); }",
        float(str(viewport["zoom"])),
    )


def audit(page: Page, label: str, console: list[str]) -> list[dict[str, str]]:
    """Audit one screen across every required viewport and zoom level."""
    found: list[dict[str, str]] = []
    for viewport in VIEWPORTS:
        resize(page, viewport)
        found.extend(
            {
                "impact": str(item.get("impact")),
                "rule": str(item.get("rule")),
                "target": str(item.get("target")),
                "viewport": str(viewport["label"]),
            }
            for item in evaluate_audit(page)
        )
    require_no_blocking_violations(label, found)
    require_clean_console(label, console)
    return found


def form_shapes(page: Page) -> tuple[list[str], list[str]]:
    """Return the method and action of every form the live page rendered."""
    raw: object = page.evaluate(
        "() => Array.from(document.querySelectorAll('form'))"
        ".map((item) => [item.getAttribute('method') || '', "
        "item.getAttribute('action') || ''])"
    )
    if not isinstance(raw, list):
        raise BrowserDriverError
    methods: list[str] = []
    actions: list[str] = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != FORM_SHAPE_LENGTH:
            raise BrowserDriverError
        methods.append(str(pair[0]))
        actions.append(str(pair[1]))
    return methods, actions


def require_post_only_forms_on(page: Page, label: str) -> None:
    """Require every rendered form on this screen to be a body-only POST."""
    methods, actions = form_shapes(page)
    require_post_only_forms(label, methods, actions)


def require_expected_screen(page: Page, expected_url: str, label: str) -> None:
    """Fail with the observed screen identity instead of a bare contract miss."""
    if page.url.split("?")[0].rstrip("/") == expected_url.rstrip("/"):
        return
    message = (
        f"{label}: expected {expected_url} but the browser is on {page.url} "
        f"titled {page.title()!r}"
    )
    raise VisualContractError(message)


def await_session(page: Page) -> None:
    """Wait for the progressive HTMX sign-in to actually establish a session."""
    deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if any(cookie["name"] == "sessionid" for cookie in page.context.cookies()):
            return
        time.sleep(SESSION_POLL_SECONDS)
    message = "sign-in never established an authenticated session"
    raise VisualContractError(message)


def sign_in(page: Page, base_url: str, config: dict[str, str]) -> None:
    """Sign the configured persona in and wait for a real session cookie."""
    sign_in_as(page, base_url, config["username"], config["password"])


def sign_in_as(page: Page, base_url: str, username: str, password: str) -> None:
    """Sign one named persona in through the progressive login form."""
    page.goto(f"{base_url}/auth/login/", wait_until="load")
    page.fill("#id_username", username)
    page.fill("#id_password", password)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    await_session(page)


def capture(
    page: Page,
    artifacts: dict[str, bytes],
    prefix: str,
    label: str,
) -> None:
    """Capture one full-page screenshot into the bounded artifact set."""
    artifacts[f"{prefix}/{label}.png"] = page.screenshot(full_page=True)
