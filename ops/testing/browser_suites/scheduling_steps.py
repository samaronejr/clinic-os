"""One clearly synthetic clinic week driven through the real staff screens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ops.testing.browser_suite_driver import (
    audit,
    capture,
    require_expected_screen,
    require_post_only_forms_on,
    sign_in_as,
)
from ops.testing.browser_visual_contract import (
    VisualContractError,
    require_no_state_in_url,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page

LOCAL_DATE: Final = "2031-03-05"
WEEK_MONDAY: Final = "2031-03-03"
PROMISED_WINDOW: Final = ("09:00", "12:00")
BOOKING_START: Final = f"{LOCAL_DATE}T09:30"
BOOKING_END: Final = f"{LOCAL_DATE}T10:15"
MOVED_START: Final = f"{LOCAL_DATE}T10:30"
MOVED_END: Final = f"{LOCAL_DATE}T11:00"
PATIENT_NAME: Final = "Otto Synthetic Testpatient"
PATIENT_BIRTH_DATE: Final = "1990-02-03"
SEARCH_TERM: Final = "Otto"
CANCELLATION_REASON: Final = "patient_request"
OK_STATUS: Final = 200


@dataclass(slots=True)
class Journey:
    """Mutable console and artifact accumulators shared by every screen."""

    base_url: str
    clinic_id: str
    prefix: str
    console: list[str]
    artifacts: dict[str, bytes]

    def clinic(self, suffix: str) -> str:
        """Return one clinic-scoped absolute URL for this journey."""
        return f"{self.base_url}/scheduling/clinics/{self.clinic_id}/{suffix}"

    def agenda_at(self, view: str, day: str, page: int = 1) -> str:
        """Return the state-free agenda URL for one civil view and date."""
        return self.clinic(f"agenda/{view}/{day}/{page}/")

    def patients(self, suffix: str = "") -> str:
        """Return one clinic-scoped intake URL for this journey."""
        return f"{self.base_url}/intake/clinics/{self.clinic_id}/patients/{suffix}"


def _screen(page: Page, journey: Journey, label: str) -> list[dict[str, str]]:
    findings = audit(page, label, journey.console)
    capture(page, journey.artifacts, journey.prefix, label)
    return findings


def _require_ok(page: Page, url: str, label: str) -> None:
    response = page.goto(url, wait_until="load")
    if response is None or response.status != OK_STATUS:
        status = "none" if response is None else str(response.status)
        message = f"{label}: {url} returned status {status}"
        raise VisualContractError(message)


def prepare_clinic(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Promise one availability window and register one synthetic patient."""
    availability = journey.clinic("availability/")
    _require_ok(page, availability, "promised-availability")
    page.select_option("#id_practitioner", index=1)
    page.fill("#id_local_date", LOCAL_DATE)
    page.fill("#id_start_time", PROMISED_WINDOW[0])
    page.fill("#id_end_time", PROMISED_WINDOW[1])
    page.click(".scheduling-card button[type=submit]")
    page.wait_for_url(availability)
    page.wait_for_selector(".scheduling-table tbody tr")
    findings = _screen(page, journey, "promised-availability")
    _require_ok(page, journey.patients("new/"), "registered-patient")
    page.fill("#id_full_name", PATIENT_NAME)
    page.fill("#id_birth_date", PATIENT_BIRTH_DATE)
    page.click("button[type=submit]")
    page.wait_for_url(journey.patients())
    return findings + _screen(page, journey, "registered-patient")


def book_from_search(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Search the registry and open the booking screen from a result row."""
    page.fill("#id_q", SEARCH_TERM)
    page.click(".intake-card form button[type=submit]")
    page.wait_for_selector(".intake-table tbody tr")
    sentinels = (SEARCH_TERM, PATIENT_BIRTH_DATE)
    require_no_state_in_url("search-result", page.url, sentinels)
    findings = _screen(page, journey, "search-result")
    page.click(".intake-table tbody tr:first-child button[type=submit]")
    page.wait_for_selector("#scheduling-booking")
    booking_url = journey.clinic("appointments/new/")
    require_expected_screen(page, booking_url, "booking-windows")
    require_post_only_forms_on(page, "booking-windows")
    require_no_state_in_url("booking-windows", page.url, (PATIENT_NAME, "enrollment"))
    panel = page.inner_text("#scheduling-booking")
    if f"{LOCAL_DATE}T{PROMISED_WINDOW[0]}" not in panel:
        message = "booking-windows: the promised availability window is missing"
        raise VisualContractError(message)
    return findings + _screen(page, journey, "booking-windows")


def create_appointment(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Book one explicit clinic-local window and land on the day agenda."""
    agenda = journey.agenda_at("day", LOCAL_DATE)
    page.select_option("#id_practitioner", index=1)
    page.fill("#id_start_local", BOOKING_START)
    page.fill("#id_end_local", BOOKING_END)
    page.click("#scheduling-booking button[type=submit]")
    page.wait_for_url(agenda)
    page.wait_for_selector(".scheduling-table tbody tr")
    require_no_state_in_url("day-agenda", page.url, (PATIENT_NAME, "enrollment"))
    if BOOKING_START not in page.inner_text(".scheduling-table"):
        message = "day-agenda: the booked window is missing from the agenda"
        raise VisualContractError(message)
    return _screen(page, journey, "day-agenda")


def read_week(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Switch to the ISO-week agenda that contains the booked civil date."""
    page.click(f"a[href$='/agenda/week/{LOCAL_DATE}/1/']")
    page.wait_for_selector(".scheduling-table tbody tr")
    require_no_state_in_url("week-agenda", page.url, (PATIENT_NAME, "enrollment"))
    if PATIENT_NAME not in page.inner_text(".scheduling-table"):
        message = "week-agenda: the booked appointment left its own ISO week"
        raise VisualContractError(message)
    return _screen(page, journey, "week-agenda")


def reschedule(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Move the appointment inside the same promised availability window."""
    page.click("a[href$='/reschedule/']")
    page.wait_for_selector("#scheduling-transition")
    findings = _screen(page, journey, "reschedule-form")
    page.fill("#id_start_local", MOVED_START)
    page.fill("#id_end_local", MOVED_END)
    page.click("#scheduling-transition button[type=submit]")
    page.wait_for_function(
        "(value) => document.querySelector('#scheduling-transition')"
        "?.textContent.includes(value)",
        arg=MOVED_START,
    )
    require_no_state_in_url("rescheduled", page.url, (PATIENT_NAME, MOVED_START))
    return findings + _screen(page, journey, "rescheduled")


def cancel(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Cancel the appointment with one closed reason and prove it is terminal."""
    _require_ok(page, journey.agenda_at("day", LOCAL_DATE), "cancelled")
    page.click("a[href$='/cancel/']")
    page.wait_for_selector("#scheduling-transition")
    findings = _screen(page, journey, "cancel-form")
    page.select_option("#id_reason", CANCELLATION_REASON)
    page.click("#scheduling-transition button[type=submit]")
    page.wait_for_selector("#transition-status")
    require_no_state_in_url("cancelled", page.url, (PATIENT_NAME, CANCELLATION_REASON))
    if page.locator("#scheduling-transition form").count():
        message = "cancelled: a terminal appointment still offered a control"
        raise VisualContractError(message)
    return findings + _screen(page, journey, "cancelled")


def rebook(page: Page, journey: Journey) -> list[dict[str, str]]:
    """Reuse the freed window to prove cancellation released the slot."""
    _require_ok(page, journey.patients(), "rebooked")
    page.fill("#id_q", SEARCH_TERM)
    page.click(".intake-card form button[type=submit]")
    page.wait_for_selector(".intake-table tbody tr")
    page.click(".intake-table tbody tr:first-child button[type=submit]")
    page.wait_for_selector("#scheduling-booking")
    page.select_option("#id_practitioner", index=1)
    page.fill("#id_start_local", BOOKING_START)
    page.fill("#id_end_local", BOOKING_END)
    page.click("#scheduling-booking button[type=submit]")
    page.wait_for_url(journey.agenda_at("day", LOCAL_DATE))
    page.wait_for_function(
        "(value) => (document.querySelector('#agenda-status')?.textContent"
        " || '').includes(value)",
        arg="2 appointments",
    )
    require_no_state_in_url("rebooked", page.url, (PATIENT_NAME, "enrollment"))
    return _screen(page, journey, "rebooked")


def without_javascript(
    browser: Browser,
    journey: Journey,
    config: dict[str, str],
) -> list[dict[str, str]]:
    """Prove the same staff journey still works with JavaScript disabled."""
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.set_default_timeout(20_000)
    try:
        sign_in_as(page, journey.base_url, config["username"], config["password"])
        _require_ok(page, journey.patients(), "javascript-disabled")
        page.fill("#id_q", SEARCH_TERM)
        page.click(".intake-card form button[type=submit]")
        page.wait_for_selector(".intake-table tbody tr")
        page.click(".intake-table tbody tr:first-child button[type=submit]")
        page.wait_for_selector("#scheduling-booking")
        booking_url = journey.clinic("appointments/new/")
        require_expected_screen(page, booking_url, "javascript-disabled")
        require_post_only_forms_on(page, "javascript-disabled")
        require_no_state_in_url("javascript-disabled", page.url, (PATIENT_NAME,))
        if f"{LOCAL_DATE}T{PROMISED_WINDOW[0]}" not in page.inner_text(
            "#scheduling-booking"
        ):
            message = "javascript-disabled: the promised window never rendered"
            raise VisualContractError(message)
        findings = _screen(page, journey, "javascript-disabled")
    finally:
        context.close()
    return findings
