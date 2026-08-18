"""Availability suite: drive the real clinic availability screen in a browser."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from playwright.sync_api import sync_playwright

from ops.testing.browser_suite_driver import (
    CONFIG_KEYS,
    audit,
    capture,
    record_console,
    record_error,
    require_expected_screen,
    require_post_only_forms_on,
    sign_in,
    sign_in_as,
)
from ops.testing.browser_visual_contract import (
    VIEWPORTS,
    VisualContractError,
    blocking_violations,
    require_no_state_in_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import Browser, Page

SUITE_ID: Final = "availability"
ARTIFACT_PREFIX: Final = "browser/availability"
NAVIGATION_TIMEOUT_MS: Final = 15_000
OK_STATUS: Final = 200
LOCAL_DATE: Final = "2031-03-04"
FIRST_WINDOW: Final = ("08:00", "09:00")
ADJACENT_WINDOW: Final = ("09:00", "10:00")
PHYSICIAN_KEYS: Final = frozenset({"physician_password", "physician_username"})
AVAILABILITY_CONFIG_KEYS: Final = CONFIG_KEYS | PHYSICIAN_KEYS
type CodeProvider = Callable[[], str]


@dataclass(slots=True)
class _Journey:
    """Mutable console and artifact accumulators shared by every screen."""

    base_url: str
    list_url: str
    console: list[str]
    artifacts: dict[str, bytes]


class AvailabilitySuiteError(RuntimeError):
    """Reject a malformed availability-suite configuration."""

    def __init__(self) -> None:
        """Expose one stable non-identifying suite configuration message."""
        super().__init__("availability browser suite configuration rejected")


def build_availability_suite(
    config: dict[str, str],
    code_provider: CodeProvider,
) -> Callable[[], dict[str, bytes]]:
    """Return the zero-argument runner entrypoint for the availability suite."""
    if set(config) != set(AVAILABILITY_CONFIG_KEYS) or any(
        not value for value in config.values()
    ):
        raise AvailabilitySuiteError

    def run() -> dict[str, bytes]:
        return run_availability_suite(config, code_provider)

    return run


def run_availability_suite(
    config: dict[str, str],
    code_provider: CodeProvider,
) -> dict[str, bytes]:
    """Execute the manager and physician journeys and return bounded artifacts."""
    base_url = config["base_url"].rstrip("/")
    journey = _Journey(
        base_url=base_url,
        list_url=(f"{base_url}/scheduling/clinics/{config['clinic_id']}/availability/"),
        console=[],
        artifacts={},
    )
    findings: list[dict[str, str]] = []

    with sync_playwright() as driver:
        browser = driver.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context()
        page = _prepare(context.new_page(), journey)
        try:
            sign_in(page, base_url, config)
            findings += _blank_screen(page, journey)
            findings += _create(page, journey, FIRST_WINDOW, 1)
            findings += _create(page, journey, ADJACENT_WINDOW, 2)
            findings += _replay(page, journey)
            findings += _retire(page, journey)
            findings += _physician(browser, journey, config, code_provider)
        finally:
            context.close()
            browser.close()

    journey.artifacts[f"{ARTIFACT_PREFIX}/summary.json"] = _summary(
        findings, journey.console
    )
    return dict(sorted(journey.artifacts.items()))


def _prepare(page: Page, journey: _Journey) -> Page:
    page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    page.on("console", lambda message: record_console(message, journey.console))
    page.on("pageerror", lambda error: record_error(error, journey.console))
    return page


def _rows(page: Page) -> int:
    raw: object = page.evaluate(
        "() => document.querySelectorAll('.scheduling-table tbody tr').length"
    )
    if not isinstance(raw, int):
        raise AvailabilitySuiteError
    return raw


def _await_rows(page: Page, expected: int) -> None:
    page.wait_for_function(
        "(count) => document.querySelectorAll("
        "'.scheduling-table tbody tr').length === count",
        arg=expected,
    )


def _blank_screen(page: Page, journey: _Journey) -> list[dict[str, str]]:
    list_url = journey.list_url
    response = page.goto(list_url, wait_until="load")
    if response is None or response.status != OK_STATUS:
        status = "none" if response is None else str(response.status)
        message = f"blank-availability: clinic screen returned status {status}"
        raise VisualContractError(message)
    require_expected_screen(page, list_url, "blank-availability")
    require_post_only_forms_on(page, "blank-availability")
    require_no_state_in_url("blank-availability", page.url, (LOCAL_DATE,))
    if _rows(page) != 0:
        message = "blank-availability: the clinic already promised availability"
        raise VisualContractError(message)
    findings = audit(page, "blank-availability", journey.console)
    capture(page, journey.artifacts, ARTIFACT_PREFIX, "blank-availability")
    return findings


def _submit(page: Page, window: tuple[str, str]) -> None:
    start_time, end_time = window
    page.select_option("#id_practitioner", index=1)
    page.fill("#id_local_date", LOCAL_DATE)
    page.fill("#id_start_time", start_time)
    page.fill("#id_end_time", end_time)
    page.click(".scheduling-card button[type=submit]")


def _create(
    page: Page,
    journey: _Journey,
    window: tuple[str, str],
    expected_rows: int,
) -> list[dict[str, str]]:
    label = f"availability-window-{expected_rows}"
    _submit(page, window)
    page.wait_for_url(journey.list_url)
    _await_rows(page, expected_rows)
    require_no_state_in_url(label, page.url, (LOCAL_DATE, window[0]))
    require_post_only_forms_on(page, label)
    findings = audit(page, label, journey.console)
    capture(page, journey.artifacts, ARTIFACT_PREFIX, label)
    return findings


def _replay(page: Page, journey: _Journey) -> list[dict[str, str]]:
    _submit(page, ADJACENT_WINDOW)
    page.wait_for_selector("#scheduling-errors")
    _await_rows(page, 2)
    require_expected_screen(page, journey.list_url, "availability-replay")
    if "already promises" not in page.inner_text("#scheduling-errors"):
        message = "availability-replay: an overlapping resubmission was accepted"
        raise VisualContractError(message)
    findings = audit(page, "availability-replay", journey.console)
    capture(page, journey.artifacts, ARTIFACT_PREFIX, "availability-replay")
    return findings


def _retire(page: Page, journey: _Journey) -> list[dict[str, str]]:
    list_url = journey.list_url
    page.goto(list_url, wait_until="load")
    _await_rows(page, 2)
    page.click(".scheduling-table tbody tr:first-child button[type=submit]")
    page.wait_for_url(list_url)
    _await_rows(page, 1)
    require_no_state_in_url("availability-retired", page.url, (LOCAL_DATE, "retire"))
    require_expected_screen(page, list_url, "availability-retired")
    findings = audit(page, "availability-retired", journey.console)
    capture(page, journey.artifacts, ARTIFACT_PREFIX, "availability-retired")
    return findings


def _physician(
    browser: Browser,
    journey: _Journey,
    config: dict[str, str],
    code_provider: CodeProvider,
) -> list[dict[str, str]]:
    list_url = journey.list_url
    context = browser.new_context()
    page = _prepare(context.new_page(), journey)
    try:
        sign_in_as(
            page,
            journey.base_url,
            config["physician_username"],
            config["physician_password"],
        )
        page.goto(list_url, wait_until="load")
        if "/auth/verify/" not in page.url:
            message = (
                "physician-challenge: an unverified physician reached "
                f"{page.url} instead of the step-up challenge"
            )
            raise VisualContractError(message)
        findings = audit(page, "physician-challenge", journey.console)
        capture(page, journey.artifacts, ARTIFACT_PREFIX, "physician-challenge")
        page.fill("#id_otp_token", code_provider())
        page.click("button[type=submit]")
        page.wait_for_url(list_url)
        require_expected_screen(page, list_url, "physician-availability")
        _await_rows(page, 1)
        if page.locator(".scheduling-card").count():
            message = "physician-availability: a physician was offered write controls"
            raise VisualContractError(message)
        findings += audit(page, "physician-availability", journey.console)
        capture(page, journey.artifacts, ARTIFACT_PREFIX, "physician-availability")
    finally:
        context.close()
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
