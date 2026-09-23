"""Browser booking, delivery receipts, opt-out/cancel and provider failure."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser._protected import encrypt
from renewal.browser.test_availability import _sign_in_receptionist, availability_staff
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_self_booking import _choose_day, _seed

if TYPE_CHECKING:
    from playwright.sync_api import Page

__all__ = ("availability_staff",)
CHANNELS = {1280: "email", 768: "sms", 375: "whatsapp"}


def _seed_contact(
    staff: dict[str, str], day: str, channel: str, name: str
) -> dict[str, str]:
    data = _seed(staff, day)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "UPDATE clinic_app.intake_patient SET full_name=%s WHERE id=%s",
            [
                encrypt(conn, "intake.patient.full_name", name.encode()),
                data["patient"],
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientcontact "
            "(id,organization_id,patient_id,channel,destination,destination_version,"
            "verified_version,verified_at,verification_method,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,1,1,now(),'staff_attested',now(),now())",
            [
                uuid4(),
                staff["organization"],
                data["patient"],
                channel,
                encrypt(
                    conn,
                    "intake.patientcontact.destination",
                    (
                        "synthetic@example.invalid"
                        if channel == "email"
                        else "+5511999990001"
                    ).encode(),
                ),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientchannelpreference "
            "(id,organization_id,clinic_id,patient_id,purpose,channel,opted_in,"
            "version,created_at,updated_at) VALUES (%s,%s,%s,%s,"
            "'appointment_reminder',%s,true,1,now(),now())",
            [
                uuid4(),
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                channel,
            ],
        )
    return data


def _book(page: Page, base: str, staff: dict[str, str], data: dict[str, str]) -> str:
    _redeem(page, base, staff["clinic_a"], data["code"])
    page.goto(base + "/patient/appointments/")
    _choose_day(page, data["day"])
    with page.expect_navigation():
        page.locator("[data-slot] button").first.click()
    expect(page.locator('[data-status="scheduled"]')).to_have_count(1)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT r.operation_id FROM clinic_app.comms_appointmentreminder r "
            "JOIN clinic_app.scheduling_appointment a ON a.id=r.appointment_id "
            "WHERE a.patient_id=%s",
            [data["patient"]],
        ).fetchone()
        assert row is not None
        return str(row[0])


def _worker(operation: str, outcome: str, root: Path) -> None:
    env = {
        **os.environ,
        "APP_DATABASE_URL": os.environ["CLINIC_RENEWAL_WORKER_DATABASE_URL"],
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "PYTHONPATH": str(Path.cwd()) + os.pathsep + str(Path.cwd() / "tests"),
    }
    result = subprocess.run(  # noqa: S603 - fixed test entrypoint and owned DSN
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from renewal.reminder_worker import main; main()",
            operation,
            outcome,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (root / f"worker-{operation}-{outcome}.log").write_text(
        result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    expected = {
        "sent": ["succeeded"],
        "delivered": ["skipped", "applied"],
        "fail": ["retry", "retry", "retry", "failed"],
        "cancelled": ["skipped"],
        "optout": ["cancelled"],
    }
    assert receipt["outcomes"] == expected[outcome]
    assert receipt["runtime_role"] == "clinic_app"


def _capture(page: Page, root: Path, state: str, width: int) -> None:
    page.screenshot(path=str(root / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("main .button").evaluate_all(
        "buttons => buttons.every(b => b.getBoundingClientRect().height >= 44)"
    )


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_reminder_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, channel = availability_staff, renewal_base_url, CHANNELS[width]
    root = renewal_artifact_root / "reminders"
    root.mkdir(exist_ok=True)
    browser = renewal_page.context.browser
    assert browser is not None
    contexts = [
        browser.new_context(
            locale="pt-BR",
            timezone_id="Asia/Tokyo",
            viewport={"width": width, "height": 900},
            reduced_motion="reduce",
        )
        for _ in range(2)
    ]
    manager, patient = [context.new_page() for context in contexts]
    errors: list[str] = []
    console: list[str] = []
    for page in (manager, patient):
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                console.append(message.text) if message.type == "error" else None
            ),
        )
    url = f"{base}/scheduling/clinics/{staff['clinic_a']}/reminders/"
    try:
        _sign_in_receptionist(manager, base, staff)
        manager.goto(url)
        _capture(manager, root, "default", width)
        _journey_states((manager, patient), (staff, base, channel, url), (root, width))
        manager.set_viewport_size({"width": 320, "height": 900})
        manager.emulate_media(forced_colors="active", reduced_motion="reduce")
        _capture(manager, root, "reflow-forced-colors", width)
        manager.set_viewport_size({"width": width // 2, "height": 500})
        _capture(manager, root, "zoom-200", width)
        manager.get_by_role("link", name="Atualizar situação").focus()
        with manager.expect_navigation():
            manager.keyboard.press("Enter")
        assert not errors
        assert not console
        (root / f"accessibility-console-{width}.json").write_text(
            json.dumps(
                {
                    "page_errors": errors,
                    "console_errors": console,
                    "reflow_320": True,
                    "forced_colors": True,
                    "reduced_motion": True,
                    "keyboard_refresh": True,
                    "target_44px": True,
                    "zoom_equivalent_half_viewport": True,
                    "channel": channel,
                    "real_provider": "waiting_external",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        for context in contexts:
            context.close()


def _open_contacts(page: Page, base: str, staff: dict[str, str], name: str) -> None:
    page.goto(f"{base}/intake/clinics/{staff['clinic_a']}/patients/")
    page.locator("#id_q").fill(name)
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as response:
        page.locator("#patient-search-form button[type=submit]").click()
    assert response.value.status == 200
    row = page.locator(".intake-table tbody tr", has_text=name)
    with page.expect_navigation():
        row.get_by_role("button", name=re.compile(r"^Contatos")).click()
    expect(page.locator(".contacts-purpose")).to_have_count(3)


def _journey_states(
    pages: tuple[Page, Page],
    inputs: tuple[dict[str, str], str, str, str],
    capture: tuple[Path, int],
) -> None:
    manager, patient = pages
    staff, base, channel, url = inputs
    root, width = capture
    for index, outcome in enumerate(("sent", "optout", "cancelled", "fail")):
        day = f"2035-08-{1 + list(CHANNELS).index(width) * 5 + index:02d}"
        name = f"Paciente Sintético Lembrete {channel} {outcome}"
        data = _seed_contact(staff, day, channel, name)
        operation = _book(patient, base, staff, data)
        manager.goto(url)
        row = manager.locator(f'[data-operation="{operation}"]')
        expect(row).to_have_attribute("data-state", "pending")
        _capture(manager, root, f"queued-{outcome}", width)
        if outcome == "optout":
            _open_contacts(manager, base, staff, name)
            form = manager.locator(
                '.contacts-purpose:has(input[value="appointment_reminder"])'
            )
            form.locator('input[name="channel"][value=""]').check()
            with manager.expect_navigation():
                form.get_by_role("button").click()
            _capture(manager, root, "opt-out-contact", width)
        elif outcome == "cancelled":
            with patient.expect_navigation():
                patient.get_by_role("button", name="Cancelar consulta").click()
            expect(patient.locator('[data-status="cancelled"]')).to_have_count(1)
        _worker(operation, outcome, root)
        manager.goto(url)
        expect(row).to_have_attribute(
            "data-state",
            {
                "sent": "succeeded",
                "optout": "cancelled",
                "cancelled": "cancelled",
                "fail": "failed",
            }[outcome],
        )
        _capture(manager, root, outcome, width)
        if outcome == "sent":
            _worker(operation, "delivered", root)
            manager.goto(url)
            expect(row).to_have_attribute("data-state", "delivered")
            expect(row.locator("[data-receipt]")).to_have_attribute(
                "data-receipt", f"synthetic:{channel}:{operation}"
            )
            _capture(manager, root, "delivered", width)


def test_native_long_content_and_denied_scope(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base = availability_staff, renewal_base_url
    root = renewal_artifact_root / "reminders"
    root.mkdir(exist_ok=True)
    browser = renewal_page.context.browser
    assert browser is not None
    contexts = [
        browser.new_context(
            locale="pt-BR",
            java_script_enabled=False,
            viewport={"width": 640, "height": 500},
            device_scale_factor=2,
        )
        for _ in range(2)
    ]
    manager, patient = [context.new_page() for context in contexts]
    try:
        data = _seed_contact(
            staff,
            "2035-08-25",
            "email",
            "Paciente Sintético " + " ".join(["Nome longo"] * 12),
        )
        operation = _book(patient, base, staff, data)
        _sign_in_receptionist(manager, base, staff)
        manager.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/reminders/")
        expect(manager.locator(f'[data-operation="{operation}"]')).to_have_attribute(
            "data-state", "pending"
        )
        _capture(manager, root, "native-long-zoom-200", 1280)
        manager.get_by_role("link", name="Atualizar situação").focus()
        with manager.expect_navigation():
            manager.keyboard.press("Enter")
        manager.set_viewport_size({"width": 320, "height": 900})
        _capture(manager, root, "native-long-reflow", 320)
        response = manager.goto(
            f"{base}/scheduling/clinics/{staff['clinic_b']}/reminders/"
        )
        assert response is not None
        assert response.status == 404
        expect(manager.locator("[data-operation]")).to_have_count(0)
        _capture(manager, root, "other-clinic-denied", 320)
    finally:
        for context in contexts:
            context.close()
