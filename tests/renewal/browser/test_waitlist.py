"""Real clinic_app cancel/offer/accept/FIFO and expired/replayed/stale journeys."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser.test_availability import _sign_in_receptionist, availability_staff
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_self_booking import _choose_day, _seed

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
PATH = "/patient/offers/"


@dataclass(frozen=True)
class _Case:
    staff: dict[str, str]
    first: dict[str, str]
    second: dict[str, str]
    base: str
    root: Path
    width: int


def _capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "waitlist"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("main button").evaluate_all(
        "buttons => buttons.every(b => b.getBoundingClientRect().height >= 44)"
    )


def _add(manager: Page, staff: dict[str, str], data: dict[str, str], day: str) -> None:
    manager.locator("#id_entry-enrollment").select_option(data["enrollment"])
    manager.locator("#id_entry-practitioner").select_option(staff["physician_a_id"])
    manager.locator("#id_entry-start_local").fill(day + "T08:00")
    manager.locator("#id_entry-end_local").fill(day + "T12:00")
    with manager.expect_navigation():
        manager.locator("#waitlist-entry-form button").click()


def _issue(
    manager: Page, staff: dict[str, str], day: str, start: str, end: str
) -> None:
    manager.locator("#id_offer-practitioner").select_option(staff["physician_a_id"])
    manager.locator("#id_offer-start_local").fill(day + "T" + start)
    manager.locator("#id_offer-end_local").fill(day + "T" + end)
    with manager.expect_navigation():
        manager.locator("#waitlist-offer-form button").click()


def _body(page: Page) -> dict[str, str]:
    body = cast(
        "dict[str, str]",
        page.locator('[data-state="pending"] form').first.evaluate(
            "form => Object.fromEntries(new FormData(form))"
        ),
    )
    return {**body, "action": "accept"}


def _post(page: Page, body: dict[str, str]) -> int:
    return int(
        page.evaluate(
            """async body => {
      const response = await fetch('/patient/offers/', {method:'POST',
        body:new URLSearchParams(body)});
      const html = await response.text();
      document.open(); document.write(html); document.close();
      return response.status;
    }""",
            body,
        )
    )


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_waitlist_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    index = [1280, 768, 375].index(width)
    day = f"2035-07-{2 + index * 2:02d}"
    first, second = (_seed(staff, day), _seed(staff, f"2035-07-{3 + index * 2:02d}"))
    case = _Case(staff, first, second, renewal_base_url, renewal_artifact_root, width)
    browser = renewal_page.context.browser
    assert browser is not None
    contexts = [
        browser.new_context(
            locale="pt-BR",
            timezone_id="Asia/Tokyo",
            viewport={"width": width, "height": 900},
            reduced_motion="reduce",
        )
        for _ in range(3)
    ]
    patient, other, manager = [context.new_page() for context in contexts]
    errors: list[str] = []
    console_errors: list[str] = []
    for page in (patient, other, manager):
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
    queue = f"{renewal_base_url}/scheduling/clinics/{staff['clinic_a']}/waitlist/"
    try:
        _cancel_opening(patient, case)
        _sign_in_receptionist(manager, renewal_base_url, staff)
        manager.goto(queue)
        _capture(manager, renewal_artifact_root, "staff-default", width)
        _add(manager, staff, first, day)
        _add(manager, staff, second, day)
        _issue(manager, staff, day, "08:00", "08:30")
        _capture(manager, renewal_artifact_root, "staff-offered", width)
        patient.goto(renewal_base_url + PATH)
        expect(patient.locator('[data-state="pending"]')).to_have_count(1)
        saved = _body(patient)
        _capture(patient, renewal_artifact_root, "patient-pending", width)
        patient.get_by_role("button", name="Aceitar e agendar").focus()
        with patient.expect_navigation():
            patient.keyboard.press("Enter")
        expect(patient.locator('[data-state="accepted"]')).to_have_count(1)
        _capture(patient, renewal_artifact_root, "accepted", width)
        assert _post(patient, saved) == 200
        expect(patient.locator('[data-state="accepted"]')).to_have_count(1)
        _capture(patient, renewal_artifact_root, "replayed", width)
        manager.goto(queue)
        expect(
            manager.locator(f'[data-entry]:has([data-offer="{saved["offer_id"]}"])')
        ).to_have_attribute("data-state", "fulfilled")
        _issue(manager, staff, day, "08:30", "09:00")
        _capture(manager, renewal_artifact_root, "queue-advanced", width)
        _expire_and_deny(other, case, saved)
        _stale_and_decline((patient, other, manager), case)
        _report(patient, case, errors, console_errors)
    finally:
        for context in contexts:
            context.close()


def _report(
    patient: Page, case: _Case, errors: list[str], console_errors: list[str]
) -> None:
    patient.set_viewport_size({"width": 320, "height": 900})
    patient.emulate_media(forced_colors="active", reduced_motion="reduce")
    _capture(patient, case.root, "reflow-forced-colors", case.width)
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        assert conn.execute(
            "SELECT count(*) FROM clinic_app.scheduling_appointment "
            "WHERE patient_id=%s AND status='scheduled'",
            [case.first["patient"]],
        ).fetchone() == (1,)
    assert not errors
    assert all("403" in message or "409" in message for message in console_errors)
    (case.root / "waitlist" / f"accessibility-console-{case.width}.json").write_text(
        json.dumps(
            {
                "page_errors": errors,
                "console_expected_http_errors": console_errors,
                "keyboard_acceptance": True,
                "button_targets_44px": True,
                "reflow_320": True,
                "forced_colors": True,
                "reduced_motion": True,
                "clinic_timezone": "America/Sao_Paulo",
                "browser_timezone": "Asia/Tokyo",
                "cancel_offer_accept_advance": True,
                "expired_replay_stale_denied": True,
            },
            indent=2,
        )
        + "\n"
    )


def _cancel_opening(patient: Page, case: _Case) -> None:
    _redeem(patient, case.base, case.staff["clinic_a"], case.first["code"])
    patient.goto(case.base + PATH)
    _capture(patient, case.root, "empty-patient", case.width)
    patient.get_by_role("link", name="Consultar outros horários").click()
    _choose_day(patient, case.first["day"])
    with patient.expect_navigation():
        patient.locator("[data-slot] button").first.click()
    expect(patient.locator('[data-status="scheduled"]')).to_have_count(1)
    with patient.expect_navigation():
        patient.get_by_role("button", name="Cancelar consulta").click()
    expect(patient.locator('[data-status="cancelled"]')).to_have_count(1)
    _capture(patient, case.root, "cancelled-opening", case.width)


def _expire_and_deny(other: Page, case: _Case, saved: dict[str, str]) -> None:
    _redeem(other, case.base, case.staff["clinic_a"], case.second["code"])
    other.goto(case.base + PATH)
    expect(other.locator('[data-state="pending"]')).to_have_count(1)
    expired = _body(other)
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        conn.execute(
            "UPDATE clinic_app.scheduling_waitlistoffer "
            "SET created_at=now()-interval '1 hour', "
            "expires_at=now()-interval '1 minute' WHERE id=%s",
            [expired["offer_id"]],
        )
    assert _post(other, expired) == 409
    expect(other.locator('[data-state="expired"]')).to_have_count(1)
    expect(other.get_by_role("alert")).to_be_visible()
    _capture(other, case.root, "expired", case.width)
    assert _post(other, expired) == 409
    assert _post(other, {**expired, "offer_id": saved["offer_id"]}) == 403
    _capture(other, case.root, "other-patient-denied", case.width)


def _stale_and_decline(pages: tuple[Page, Page, Page], case: _Case) -> None:
    patient, other, manager = pages
    staff, first, day = case.staff, case.first, case.first["day"]
    base, root, width = case.base, case.root, case.width
    _add(manager, staff, first, day)
    _issue(manager, staff, day, "09:00", "09:30")
    patient.goto(base + PATH)
    declined = _body(patient)
    with patient.expect_navigation():
        patient.get_by_role("button", name="Recusar oferta").click()
    expect(patient.locator('[data-state="declined"]')).to_have_count(1)
    assert _post(patient, declined) == 409
    _capture(patient, root, "declined-replay", width)
    _add(manager, staff, first, day)
    _issue(manager, staff, day, "09:00", "09:30")
    patient.goto(base + PATH)
    stale = _body(patient)
    # Another patient books the offered opening through the real booking surface.
    other.goto(base + "/patient/appointments/?day=" + day)
    slot = other.locator("[data-slot]").filter(
        has=other.get_by_text("09:00\N{EN DASH}09:30", exact=True)
    )
    with other.expect_navigation():
        slot.get_by_role("button").click()
    expect(other.locator('[data-status="scheduled"]')).to_have_count(1)
    assert _post(patient, stale) == 409
    expect(patient.locator('[data-state="unavailable"]')).to_have_count(1)
    expect(
        patient.get_by_role("link", name="Consultar outros horários")
    ).to_be_visible()
    _capture(patient, root, "stale-slot-recovery", width)


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_native_long_content_zoom(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    day = f"2035-07-{20 + [1280, 768, 375].index(width):02d}"
    data = _seed(staff, day)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        java_script_enabled=False,
        viewport={"width": width // 2, "height": 500},
        device_scale_factor=2,
    )
    manager_context = browser.new_context(
        locale="pt-BR", viewport={"width": width // 2, "height": 500}
    )
    page, manager = context.new_page(), manager_context.new_page()
    try:
        with psycopg.connect(staff["dsn"]) as conn:
            conn.execute(
                "UPDATE clinic_app.identity_user SET username=%s WHERE id=%s",
                ["ProfissionalSintetico" * 7, staff["physician_a_id"]],
            )
        _sign_in_receptionist(manager, renewal_base_url, staff)
        manager.goto(
            f"{renewal_base_url}/scheduling/clinics/{staff['clinic_a']}/waitlist/"
        )
        _add(manager, staff, data, day)
        _issue(manager, staff, day, "08:00", "08:30")
        _redeem(page, renewal_base_url, staff["clinic_a"], data["code"])
        page.goto(renewal_base_url + PATH)
        _capture(page, renewal_artifact_root, "native-long-zoom-200", width)
        page.get_by_role("button", name="Aceitar e agendar").focus()
        with page.expect_navigation():
            page.keyboard.press("Enter")
        expect(page.locator('[data-state="accepted"]')).to_have_count(1)
        _capture(page, renewal_artifact_root, "native-accepted-zoom-200", width)
    finally:
        context.close()
        manager_context.close()
