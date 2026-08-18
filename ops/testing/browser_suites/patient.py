"""Patient suite: drive the real POST-only search screen in a live browser."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from playwright.sync_api import Page, sync_playwright

from ops.testing.browser_suite_driver import (
    CONFIG_KEYS,
    audit,
    capture,
    record_console,
    record_error,
    require_expected_screen,
    require_post_only_forms_on,
    sign_in,
)
from ops.testing.browser_visual_contract import (
    VIEWPORTS,
    VisualContractError,
    blocking_violations,
    require_no_state_in_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SUITE_ID: Final = "patient"
ARTIFACT_PREFIX: Final = "browser/patient"
SEARCH_TERM: Final = "Marina"
NAVIGATION_TIMEOUT_MS: Final = 15_000
OK_STATUS: Final = 200


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
        page.on("console", lambda message: record_console(message, console))
        page.on("pageerror", lambda error: record_error(error, console))
        try:
            sign_in(page, base_url, config)
            findings += _blank_list(page, list_url, console, artifacts)
            findings += _search(page, list_url, console, artifacts)
            findings += _paginate(page, list_url, console, artifacts)
            findings += _create(page, create_url, list_url, console, artifacts)
        finally:
            context.close()
            browser.close()

    artifacts[f"{ARTIFACT_PREFIX}/summary.json"] = _summary(findings, console)
    return dict(sorted(artifacts.items()))


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
    require_expected_screen(page, list_url, "blank-list")
    require_post_only_forms_on(page, "blank-list")
    require_no_state_in_url("blank-list", page.url, (SEARCH_TERM,))
    findings = audit(page, "blank-list", console)
    capture(page, artifacts, ARTIFACT_PREFIX, "blank-list")
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
    findings = audit(page, "search-results", console)
    capture(page, artifacts, ARTIFACT_PREFIX, "search-results")
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
    findings = audit(page, "search-page-two", console)
    capture(page, artifacts, ARTIFACT_PREFIX, "search-page-two")
    return findings


def _create(
    page: Page,
    create_url: str,
    list_url: str,
    console: list[str],
    artifacts: dict[str, bytes],
) -> list[dict[str, str]]:
    page.goto(create_url, wait_until="load")
    findings = audit(page, "create-form", console)
    capture(page, artifacts, ARTIFACT_PREFIX, "create-form")
    page.fill("#id_full_name", "Zoe Synthetic Testpatient")
    page.fill("#id_birth_date", "1993-08-09")
    page.click("button[type=submit]")
    page.wait_for_url(list_url)
    require_no_state_in_url("create-redirect", page.url, ("Zoe", "1993-08-09"))
    findings += audit(page, "create-redirect", console)
    capture(page, artifacts, ARTIFACT_PREFIX, "create-redirect")
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
