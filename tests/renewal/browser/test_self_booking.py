"""Real invitation redemption and patient/staff booking through clinic_app."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser._protected import encrypt
from renewal.browser.test_availability import _sign_in_receptionist, availability_staff
from renewal.browser.test_patient_access import _redeem

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page, Route

__all__ = ("availability_staff",)
PATH = "/patient/appointments/"


def _seed(staff: dict[str, str], day: str) -> dict[str, str]:
    data = {
        key: str(uuid4()) for key in ("patient", "enrollment", "grant", "availability")
    }
    data["code"] = secrets.token_urlsafe(32)
    data["day"] = day
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patient "
            "(id,organization_id,full_name,birth_date,created_at) "
            "VALUES (%s,%s,%s,%s,now())",
            [
                data["patient"],
                staff["organization"],
                encrypt(
                    conn,
                    "intake.patient.full_name",
                    "Paciente Sintético Agendamento".encode(),
                ),
                encrypt(conn, "intake.patient.birth_date", b"1990-01-01"),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientclinicenrollment "
            "(id,organization_id,clinic_id,patient_id,idempotency_key,"
            "create_fingerprint,created_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
            [
                data["enrollment"],
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientaccessgrant "
            "(id,organization_id,clinic_id,patient_id,enrollment_id,issued_by_id,"
            "issued_by_label,secret_hash,operations,expires_at,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'Synthetic',%s,"
            "ARRAY['enrollment_view','booking'],now()+interval '24 hours',now())",
            [
                data["grant"],
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                data["enrollment"],
                staff["receptionist_id"],
                hashlib.sha256(data["code"].encode()).digest(),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock "
            "(id,organization_id,clinic_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
            [
                data["availability"],
                staff["organization"],
                staff["clinic_a"],
                staff["physician_a_id"],
                day + " 11:00+00",
                day + " 15:00+00",
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
    return data


def _capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "self-booking"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _choose_day(page: Page, day: str) -> None:
    page.locator("#booking-day").fill(day)
    with page.expect_navigation():
        page.get_by_role("button", name="Ver horários").click()


def _body(page: Page) -> dict[str, str]:
    return cast(
        "dict[str, str]",
        page.locator("[data-slot]").first.evaluate(
            "form => Object.fromEntries(new FormData(form))"
        ),
    )


def _post(page: Page, body: dict[str, str]) -> int:
    return int(
        page.evaluate(
            """async body => {
      const r = await fetch('/patient/appointments/', {method:'POST',
        body:new URLSearchParams(body)});
      const html = await r.text();
      document.open(); document.write(html); document.close();
      return r.status;
    }""",
            body,
        )
    )


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_patient_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    day = {1280: "2035-06-02", 768: "2035-06-03", 375: "2035-06-04"}[width]
    data = _seed(staff, day)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _redeem(page, renewal_base_url, staff["clinic_a"], data["code"])
        page.get_by_role("link", name="Suas consultas").click()
        _capture(page, renewal_artifact_root, "empty", width)
        _choose_day(page, day)
        expect(page.locator("[data-slot]")).to_have_count(8)
        _capture(page, renewal_artifact_root, "eligible", width)
        body = _body(page)
        body["action"] = "book"
        page.locator("[data-slot] button").first.focus()
        with page.expect_navigation():
            page.keyboard.press("Enter")
        expect(page.locator("[data-appointment]")).to_have_count(1)
        _capture(page, renewal_artifact_root, "booked", width)
        assert _post(page, body) == 200
        expect(page.locator("[data-appointment]")).to_have_count(1)
        _capture(page, renewal_artifact_root, "repeat-safe", width)
        _choose_day(page, day)
        with page.expect_navigation():
            page.get_by_role("button", name="Escolher novo horário").click()
        expect(page.locator('[name="action"][value="reschedule"]')).to_have_count(8)
        with page.expect_navigation():
            page.locator("[data-slot] button").nth(2).click()
        expect(page.locator("[data-appointment]")).to_have_count(1)
        _capture(page, renewal_artifact_root, "rescheduled", width)
        with page.expect_navigation():
            page.get_by_role("button", name="Cancelar consulta").click()
        expect(page.locator('[data-status="cancelled"]')).to_have_count(1)
        _capture(page, renewal_artifact_root, "cancelled", width)
        _failure_journey(page, renewal_artifact_root, staff, data)
        page.set_viewport_size({"width": 320, "height": 900})
        page.emulate_media(forced_colors="active", reduced_motion="reduce")
        _capture(page, renewal_artifact_root, "reflow-forced-colors", width)
        assert not errors
        (
            renewal_artifact_root
            / "self-booking"
            / f"accessibility-console-{width}.json"
        ).write_text(
            json.dumps(
                {
                    "page_errors": errors,
                    "keyboard_submission": True,
                    "clinic_timezone": "America/Sao_Paulo",
                    "browser_timezone": "Asia/Tokyo",
                    "reflow_320": True,
                    "forced_colors": True,
                    "reduced_motion": True,
                }
            )
        )
    finally:
        context.close()


def _failure_journey(
    page: Page, root: Path, staff: dict[str, str], data: dict[str, str]
) -> None:
    width = page.viewport_size["width"] if page.viewport_size else 1280
    _choose_day(page, data["day"])
    tampered = {
        **_body(page),
        "action": "book",
        "enrollment_id": str(uuid4()),
        "practitioner_id": staff["physician_b_id"],
    }
    url = page.url
    assert _post(page, tampered) == 403
    _capture(page, root, "scope-denied", width)
    page.goto(url)
    stale = {**_body(page), "action": "book"}
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        conn.execute(
            "UPDATE clinic_app.scheduling_availabilityblock "
            "SET retired_at=now() WHERE id=%s",
            [data["availability"]],
        )
    assert _post(page, stale) == 409
    expect(page.get_by_role("alert")).to_be_visible()
    _capture(page, root, "stale-slot", width)


def _prepare_staff(
    manager: Page, base_url: str, staff: dict[str, str], data: dict[str, str]
) -> dict[str, str]:
    _sign_in_receptionist(manager, base_url, staff)
    manager.goto(f"{base_url}/intake/clinics/{staff['clinic_a']}/patients/")
    manager.locator("#id_q").fill("Paciente Sintético Agendamento")
    with manager.expect_response(lambda response: response.request.method == "POST"):
        manager.locator("#patient-search-form button[type=submit]").click()
    row = manager.locator("tr").filter(
        has=manager.locator(f'input[value="{data["enrollment"]}"]')
    )
    with manager.expect_navigation():
        row.get_by_role("button", name="Agendar consulta").click()
    manager.locator("#id_practitioner").select_option(staff["physician_a_id"])
    manager.locator("#id_start_local").fill(data["day"] + "T08:00")
    manager.locator("#id_end_local").fill(data["day"] + "T08:30")
    return cast(
        "dict[str, str]",
        manager.locator("#scheduling-booking form").evaluate(
            "form => Object.fromEntries(new FormData(form))"
        ),
    )


def test_staff_patient_race(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    data = _seed(staff, "2035-06-05")
    browser = renewal_page.context.browser
    assert browser is not None
    # Routed requests: WebKit's route() misses service-worker-controlled
    # pages (engines.py, Request interception).
    patient_context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}, service_workers="block"
    )
    staff_context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}, service_workers="block"
    )
    patient = patient_context.new_page()
    manager = staff_context.new_page()
    try:
        _redeem(patient, renewal_base_url, staff["clinic_a"], data["code"])
        patient.goto(renewal_base_url + PATH + "?day=" + data["day"])
        patient_body = {**_body(patient), "action": "book"}
        staff_body = _prepare_staff(manager, renewal_base_url, staff, data)
        booking_url = (
            f"{renewal_base_url}/scheduling/clinics/"
            f"{staff['clinic_a']}/appointments/new/"
        )
        held: list[Route] = []

        def hold_submit(route: Route) -> None:
            if route.request.method == "POST":
                held.append(route)
                route.request.frame.evaluate("console.debug('booking-submit-held')")
            else:
                route.continue_()

        patient.route("**/patient/appointments/", hold_submit)
        manager.route("**/appointments/new/", hold_submit)
        launch = """([url, body]) => { window.bookingResult = fetch(url, {
          method:'POST',body:new URLSearchParams(body)})
          .then(async r => ({status:r.status,html:await r.text()})); }"""
        with patient.expect_console_message(
            predicate=lambda message: message.text == "booking-submit-held"
        ):
            patient.evaluate(launch, [renewal_base_url + PATH, patient_body])
        with manager.expect_console_message(
            predicate=lambda message: message.text == "booking-submit-held"
        ):
            manager.evaluate(launch, [booking_url, staff_body])
        assert len(held) == 2
        for route in held:
            route.continue_()
        # Redirected requests are not part of the submit barrier.
        patient.unroute("**/patient/appointments/")
        manager.unroute("**/appointments/new/")
        patient_result = patient.evaluate("window.bookingResult")
        manager_result = manager.evaluate("window.bookingResult")
        assert patient_result["status"] in {200, 409}
        assert manager_result["status"] == 200
        patient.set_content(patient_result["html"])
        manager.set_content(manager_result["html"])
        _capture(patient, renewal_artifact_root, "race-patient", 1280)
        _capture(manager, renewal_artifact_root, "race-staff", 1280)
        with psycopg.connect(staff["dsn"]) as conn:
            conn.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [staff["organization"]],
            )
            assert conn.execute(
                "SELECT count(*) FROM clinic_app.scheduling_appointment "
                "WHERE patient_id=%s AND status='scheduled'",
                [data["patient"]],
            ).fetchone() == (1,)
        (renewal_artifact_root / "self-booking" / "race.json").write_text(
            json.dumps(
                {
                    "barrier_requests": 2,
                    "patient_status": patient_result["status"],
                    "staff_status": manager_result["status"],
                    "surviving_bookings": 1,
                }
            )
        )
    finally:
        patient_context.close()
        staff_context.close()


@pytest.mark.parametrize(
    "screen", [(width, zoom) for zoom in (1, 2) for width in (1280, 768, 375)]
)
def test_native_long_content_zoom(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    screen: tuple[int, int],
) -> None:
    width, zoom = screen
    staff = availability_staff
    index = [1280, 768, 375].index(width) + (zoom - 1) * 3
    day = f"2035-06-{10 + index:02d}"
    data = _seed(staff, day)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "UPDATE clinic_app.identity_user SET username=%s WHERE id=%s",
            ["MedicoSintetico" * 10, staff["physician_a_id"]],
        )
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        java_script_enabled=False,
        viewport={"width": width // zoom, "height": 900 // zoom},
        device_scale_factor=zoom,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    try:
        _redeem(page, renewal_base_url, staff["clinic_a"], data["code"])
        page.get_by_role("link", name="Suas consultas").click()
        _choose_day(page, day)
        _capture(page, renewal_artifact_root, f"native-long-zoom-{zoom}", width)
        page.locator("[data-slot] button").first.focus()
        with page.expect_navigation():
            page.keyboard.press("Enter")
        expect(page.locator('[data-status="scheduled"]')).to_have_count(1)
        _capture(page, renewal_artifact_root, f"native-booked-zoom-{zoom}", width)
        assert not errors
        (
            renewal_artifact_root / "self-booking" / f"native-zoom-{zoom}-{width}.json"
        ).write_text(
            json.dumps(
                {
                    "errors": errors,
                    "javascript_enabled": False,
                    "keyboard_submission": True,
                    "device_scale_factor": zoom,
                    "css_width": width // zoom,
                    "overflow": False,
                }
            )
        )
    finally:
        context.close()
