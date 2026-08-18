"""Scheduling suite: drive the real booking, agenda, and transition screens.

The journey is one clearly synthetic clinic week: promise availability, register
a patient, book an explicit clinic-local window from the search result, read the
day and week agendas, move the appointment, cancel it, reuse the freed window,
and finally prove a physician sees an own-scope read-only agenda.
"""

from __future__ import annotations

import json
import time
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
    sign_in,
    sign_in_as,
)
from ops.testing.browser_suites.scheduling_steps import (
    LOCAL_DATE,
    PATIENT_NAME,
    Journey,
    book_from_search,
    cancel,
    create_appointment,
    prepare_clinic,
    read_week,
    rebook,
    reschedule,
    without_javascript,
)
from ops.testing.browser_totp_code import BrowserTotpError
from ops.testing.browser_visual_contract import (
    VIEWPORTS,
    VisualContractError,
    blocking_violations,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import Browser, Page

SUITE_ID: Final = "scheduling"
ARTIFACT_PREFIX: Final = "browser/scheduling"
NAVIGATION_TIMEOUT_MS: Final = 20_000
CODE_ATTEMPTS: Final = 15
CODE_RETRY_SECONDS: Final = 3.0
PHYSICIAN_KEYS: Final = frozenset({"physician_password", "physician_username"})
SCHEDULING_CONFIG_KEYS: Final = CONFIG_KEYS | PHYSICIAN_KEYS
type CodeProvider = Callable[[], str]


class SchedulingSuiteError(RuntimeError):
    """Reject a malformed scheduling-suite configuration."""

    def __init__(self) -> None:
        """Expose one stable non-identifying suite configuration message."""
        super().__init__("scheduling browser suite configuration rejected")


@dataclass(frozen=True, slots=True)
class _Physician:
    """Bind the physician persona to the code provider that verifies it."""

    username: str
    password: str
    code_provider: CodeProvider


def build_scheduling_suite(
    config: dict[str, str],
    code_provider: CodeProvider,
) -> Callable[[], dict[str, bytes]]:
    """Return the zero-argument runner entrypoint for the scheduling suite."""
    if set(config) != set(SCHEDULING_CONFIG_KEYS) or any(
        not value for value in config.values()
    ):
        raise SchedulingSuiteError

    def run() -> dict[str, bytes]:
        return run_scheduling_suite(config, code_provider)

    return run


def run_scheduling_suite(
    config: dict[str, str],
    code_provider: CodeProvider,
) -> dict[str, bytes]:
    """Execute the manager and physician journeys and return bounded artifacts."""
    base_url = config["base_url"].rstrip("/")
    clinic_id = config["clinic_id"]
    journey = Journey(
        base_url=base_url,
        clinic_id=clinic_id,
        prefix=ARTIFACT_PREFIX,
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
            findings += prepare_clinic(page, journey)
            findings += book_from_search(page, journey)
            findings += create_appointment(page, journey)
            findings += read_week(page, journey)
            findings += reschedule(page, journey)
            findings += cancel(page, journey)
            findings += rebook(page, journey)
            findings += without_javascript(browser, journey, config)
            findings += _physician(
                browser,
                journey,
                _Physician(
                    username=config["physician_username"],
                    password=config["physician_password"],
                    code_provider=code_provider,
                ),
            )
        finally:
            context.close()
            browser.close()

    journey.artifacts[f"{ARTIFACT_PREFIX}/summary.json"] = _summary(
        findings, journey.console
    )
    return dict(sorted(journey.artifacts.items()))


def _prepare(page: Page, journey: Journey) -> Page:
    page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    page.on("console", lambda message: record_console(message, journey.console))
    page.on("pageerror", lambda error: record_error(error, journey.console))
    return page


def _physician(
    browser: Browser,
    journey: Journey,
    persona: _Physician,
) -> list[dict[str, str]]:
    agenda_url = journey.agenda_at("day", LOCAL_DATE)
    context = browser.new_context()
    page = _prepare(context.new_page(), journey)
    try:
        sign_in_as(page, journey.base_url, persona.username, persona.password)
        page.goto(agenda_url, wait_until="load")
        if "/auth/verify/" not in page.url:
            message = (
                "physician-challenge: an unverified physician reached "
                f"{page.url} instead of the step-up challenge"
            )
            raise VisualContractError(message)
        findings = audit(page, "physician-challenge", journey.console)
        capture(page, journey.artifacts, ARTIFACT_PREFIX, "physician-challenge")
        page.fill("#id_otp_token", _fresh_code(persona.code_provider))
        page.click("button[type=submit]")
        page.wait_for_url(agenda_url)
        require_expected_screen(page, agenda_url, "physician-agenda")
        _require_read_only(page)
        findings += audit(page, "physician-agenda", journey.console)
        capture(page, journey.artifacts, ARTIFACT_PREFIX, "physician-agenda")
    finally:
        context.close()
    return findings


def _fresh_code(provider: CodeProvider) -> str:
    """Wait out the counter rather than replaying a consumed TOTP step."""
    for _attempt in range(CODE_ATTEMPTS):
        try:
            return provider()
        except BrowserTotpError:
            time.sleep(CODE_RETRY_SECONDS)
    return provider()


def _require_read_only(page: Page) -> None:
    if page.locator("form").count() or page.locator("a[href$='/cancel/']").count():
        message = "physician-agenda: a physician was offered scheduling controls"
        raise VisualContractError(message)
    if PATIENT_NAME not in page.inner_text(".scheduling-table"):
        message = "physician-agenda: the physician's own appointment is missing"
        raise VisualContractError(message)


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
