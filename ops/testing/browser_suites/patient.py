"""Patient suite: drive the real POST-only search screen in a live browser."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Final

from playwright.sync_api import ConsoleMessage, Error, Page, sync_playwright

from ops.testing.browser_visual_contract import (
    AUDIT_SCRIPT,
    VIEWPORTS,
    VisualContractError,
    blocking_violations,
    require_clean_console,
    require_no_blocking_violations,
    require_no_state_in_url,
    require_post_only_forms,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SUITE_ID: Final = "patient"
ARTIFACT_PREFIX: Final = "browser/patient"
SEARCH_TERM: Final = "Marina"
CONSOLE_LEVELS: Final = frozenset({"error", "warning"})
NAVIGATION_TIMEOUT_MS: Final = 15_000
SESSION_TIMEOUT_SECONDS: Final = 15
SESSION_POLL_SECONDS: Final = 0.1
OK_STATUS: Final = 200
CONFIG_KEYS: Final = frozenset({"base_url", "clinic_id", "password", "username"})
FORM_SHAPE_LENGTH: Final = 2


class PatientSuiteError(RuntimeError):
    """Reject a malformed patient-suite configuration or audit response."""

    def __init__(self) -> None:
        """Expose one stable non-identifying suite configuration message."""
        super().__init__("patient browser suite configuration rejected")


def build_patient_suite(config: dict[str, str]) -> Callable[[], dict[str, bytes]]:
    """Return the zero-argument runner entrypoint for the patient suite."""
    if set(config) != set(CONFIG_KEYS) or any(not value for value in config.values()):
        raise PatientSuiteError

    def run() -> dict[str, bytes]:
        return run_patient_suite(config)

    return run


def run_patient_suite(config: dict[str, str]) -> dict[str, bytes]:
    """Execute the full receptionist journey and return bounded artifacts."""
    base_url = config["base_url"].rstrip("/")
    list_url = f"{base_url}/intake/clinics/{config['clinic_id']}/patients/"
    create_url = f"{list_url}new/"
    artifacts: dict[str, bytes] = {}
    console: list[str] = []
    findings: list[dict[str, str]] = []

    with sync_playwright() as driver:
        browser = driver.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
        page.on("console", lambda message: _record_console(message, console))
        page.on("pageerror", lambda error: _record_error(error, console))
        try:
            _sign_in(page, base_url, config)
            findings += _blank_list(page, list_url, console, artifacts)
            findings += _search(page, list_url, console, artifacts)
            findings += _paginate(page, list_url, console, artifacts)
            findings += _create(page, create_url, list_url, console, artifacts)
        finally:
            context.close()
            browser.close()

    artifacts[f"{ARTIFACT_PREFIX}/summary.json"] = _summary(findings, console)
    return dict(sorted(artifacts.items()))


def _record_console(message: ConsoleMessage, console: list[str]) -> None:
    if message.type in CONSOLE_LEVELS:
        console.append(f"{message.type}:{message.text}")


def _record_error(error: Error, console: list[str]) -> None:
    console.append(f"pageerror:{error.message}")


def _evaluate_audit(page: Page) -> list[dict[str, object]]:
    raw: object = page.evaluate(AUDIT_SCRIPT)
    if not isinstance(raw, list):
        raise PatientSuiteError
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PatientSuiteError
        result.append({str(key): value for key, value in item.items()})
    return result


def _resize(page: Page, viewport: dict[str, object]) -> None:
    page.set_viewport_size(
        {"width": int(str(viewport["width"])), "height": int(str(viewport["height"]))}
    )
    page.evaluate(
        "(factor) => { document.documentElement.style.zoom = String(factor); }",
        float(str(viewport["zoom"])),
    )


def _audit(page: Page, label: str, console: list[str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for viewport in VIEWPORTS:
        _resize(page, viewport)
        found.extend(
            {
                "impact": str(item.get("impact")),
                "rule": str(item.get("rule")),
                "target": str(item.get("target")),
                "viewport": str(viewport["label"]),
            }
            for item in _evaluate_audit(page)
        )
    require_no_blocking_violations(label, found)
    require_clean_console(label, console)
    return found


def _form_shapes(page: Page) -> tuple[list[str], list[str]]:
    raw: object = page.evaluate(
        "() => Array.from(document.querySelectorAll('form'))"
        ".map((item) => [item.getAttribute('method') || '', "
        "item.getAttribute('action') || ''])"
    )
    if not isinstance(raw, list):
        raise PatientSuiteError
    methods: list[str] = []
    actions: list[str] = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != FORM_SHAPE_LENGTH:
            raise PatientSuiteError
        methods.append(str(pair[0]))
        actions.append(str(pair[1]))
    return methods, actions


def _require_expected_screen(page: Page, expected_url: str, label: str) -> None:
    """Fail with the observed screen identity instead of a bare contract miss."""
    if page.url.split("?")[0].rstrip("/") == expected_url.rstrip("/"):
        return
    message = (
        f"{label}: expected {expected_url} but the browser is on {page.url} "
        f"titled {page.title()!r}"
    )
    raise VisualContractError(message)


def _sign_in(page: Page, base_url: str, config: dict[str, str]) -> None:
    page.goto(f"{base_url}/auth/login/", wait_until="load")
    page.fill("#id_username", config["username"])
    page.fill("#id_password", config["password"])
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
    _await_session(page)


def _await_session(page: Page) -> None:
    """Wait for the progressive HTMX sign-in to actually establish a session."""
    deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if any(cookie["name"] == "sessionid" for cookie in page.context.cookies()):
            return
        time.sleep(SESSION_POLL_SECONDS)
    message = "sign-in never established an authenticated session"
    raise VisualContractError(message)


def _capture(page: Page, artifacts: dict[str, bytes], label: str) -> None:
    artifacts[f"{ARTIFACT_PREFIX}/{label}.png"] = page.screenshot(full_page=True)


def _blank_list(
    page: Page,
    list_url: str,
    console: list[str],
    artifacts: dict[str, bytes],
) -> list[dict[str, str]]:
    response = page.goto(list_url, wait_until="load")
    if response is None or response.status != OK_STATUS:
        status = "none" if response is None else str(response.status)
        message = f"blank-list: clinic patient list returned status {status}"
        raise VisualContractError(message)
    _require_expected_screen(page, list_url, "blank-list")
    methods, actions = _form_shapes(page)
    require_post_only_forms("blank-list", methods, actions)
    require_no_state_in_url("blank-list", page.url, (SEARCH_TERM,))
    findings = _audit(page, "blank-list", console)
    _capture(page, artifacts, "blank-list")
    return findings


def _search(
    page: Page,
    list_url: str,
    console: list[str],
    artifacts: dict[str, bytes],
) -> list[dict[str, str]]:
    page.fill("#id_q", SEARCH_TERM)
    page.click("button[type=submit]")
    page.wait_for_selector(".intake-table")
    require_no_state_in_url("search", page.url, (SEARCH_TERM, "birth_date"))
    if page.url.rstrip("/") != list_url.rstrip("/"):
        message = "search navigated away from the clinic list URL"
        raise VisualContractError(message)
    findings = _audit(page, "search-results", console)
    _capture(page, artifacts, "search-results")
    return findings


def _paginate(
    page: Page,
    list_url: str,
    console: list[str],
    artifacts: dict[str, bytes],
) -> list[dict[str, str]]:
    first_row = page.inner_text(".intake-table tbody tr:first-child")
    page.click("form:has(input[name='page'][value='2']) button[type=submit]")
    page.wait_for_function(
        "() => (document.querySelector('#patient-results-status')?.textContent"
        " || '').includes('Page 2 of')"
    )
    page.wait_for_selector(".intake-table")
    if page.inner_text(".intake-table tbody tr:first-child") == first_row:
        message = "pagination did not advance beyond the first result page"
        raise VisualContractError(message)
    if "Previous page" not in page.inner_text(".intake-pagination"):
        message = "second result page did not offer a previous-page control"
        raise VisualContractError(message)
    require_no_state_in_url("pagination", page.url, (SEARCH_TERM, "page="))
    if page.url.rstrip("/") != list_url.rstrip("/"):
        message = "pagination navigated away from the clinic list URL"
        raise VisualContractError(message)
    findings = _audit(page, "search-page-two", console)
    _capture(page, artifacts, "search-page-two")
    return findings


def _create(
    page: Page,
    create_url: str,
    list_url: str,
    console: list[str],
    artifacts: dict[str, bytes],
) -> list[dict[str, str]]:
    page.goto(create_url, wait_until="load")
    findings = _audit(page, "create-form", console)
    _capture(page, artifacts, "create-form")
    page.fill("#id_full_name", "Zoe Synthetic Testpatient")
    page.fill("#id_birth_date", "1993-08-09")
    page.click("button[type=submit]")
    page.wait_for_url(list_url)
    require_no_state_in_url("create-redirect", page.url, ("Zoe", "1993-08-09"))
    findings += _audit(page, "create-redirect", console)
    _capture(page, artifacts, "create-redirect")
    return findings


def _summary(findings: list[dict[str, str]], console: list[str]) -> bytes:
    document = {
        "blocking_violation_count": len(blocking_violations(findings)),
        "console_messages": sorted(set(console)),
        "schema_version": 1,
        "suite_id": SUITE_ID,
        "total_violation_count": len(findings),
        "viewports": [str(item["label"]) for item in VIEWPORTS],
        "violations": sorted(
            findings,
            key=lambda item: (item["viewport"], item["rule"], item["target"]),
        ),
    }
    return json.dumps(document, sort_keys=True, indent=2).encode() + b"\n"
