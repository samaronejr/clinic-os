"""Scoped teleconsult sessions: real clinic_app room creation and denials."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser._protected import encrypt
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_retention import (
    post_action,
    seed_manager,
    sign_in_manager,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

__all__ = ("availability_staff",)
DAYS = {1280: "2035-07-06", 768: "2035-07-07", 375: "2035-07-08"}
TEXT = (
    "Autorizo o atendimento por teleconsulta nesta clínica. "
    "A sessão não é gravada nem transcrita e posso revogar esta "
    "autorização antes de novos usos.\n\n"
    + "Informação complementar sintética para leitura: "
    * 12
)


def _seed(staff: dict[str, str], day: str, hour: int) -> dict[str, str]:
    """Seed one patient, enrollment, teleconsult grant and appointment."""
    data = {key: str(uuid4()) for key in ("patient", "enrollment", "grant")}
    data["code"] = secrets.token_urlsafe(32)
    data["appointment"] = str(uuid4())
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
                    "Paciente Sintético Teleconsulta".encode(),
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
            "ARRAY['enrollment_view','consent','teleconsult'],"
            "now()+interval '24 hours',now())",
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
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                staff["physician_a_id"],
                f"{day}T{hour:02d}:00:00Z",
                f"{day}T{hour:02d}:30:00Z",
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.scheduling_appointment "
            "(id,organization_id,clinic_id,patient_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'scheduled',now(),now())",
            [
                data["appointment"],
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                staff["physician_a_id"],
                f"{day}T{hour:02d}:00:00Z",
                f"{day}T{hour:02d}:30:00Z",
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
    return data


def _capture(page: Page, root: Path, state: str, width: int) -> None:
    page.screenshot(path=str(root / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _press(page: Page, action: str) -> None:
    with page.expect_navigation():
        page.locator(f'button[value="{action}"]').first.click()


def _publish_consent(page: Page, url: str) -> None:
    page.goto(url)
    page.locator("#id_text").fill(TEXT)
    page.locator("#id_purpose").select_option("teleconsultation")
    _press(page, "publish")
    expect(page.locator('[role="status"]')).to_be_visible()


def _accept_consent(patient: Page, base: str) -> None:
    patient.goto(f"{base}/patient/consent/")
    form = patient.locator("form", has_text="Teleconsulta").first
    with patient.expect_navigation():
        form.locator('button[value="read"]').click()
    patient.locator("#id_accepted").check()
    _press(patient, "accept")
    expect(patient.locator('[role="status"]')).to_be_visible()


def _open_encounter(
    physician: Page,
    base: str,
    staff: dict[str, str],
    day: str,
    appointment_id: str,
) -> None:
    physician.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{day}/1/")
    button = physician.locator(
        'form:has(input[name="appointment_id"][value="'
        + appointment_id
        + '"]) button[value="open"]'
    )
    with physician.expect_navigation():
        button.click()


def _room_operation(staff: dict[str, str], session_id: str) -> str:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT operation_id FROM clinic_app.teleconsult_teleconsultroom "
            "WHERE session_id=%s",
            [session_id],
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
            "from renewal.teleconsult_worker import main; main()",
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
    expected = {"sent": ["succeeded"], "fail": ["retry", "retry", "retry", "failed"]}
    assert receipt["outcomes"] == expected[outcome]
    assert receipt["runtime_role"] == "clinic_app"


def _create_session(physician: Page, url: str) -> str:
    with physician.expect_navigation():
        physician.locator('button[value="create"]').first.click()
    session = physician.locator("[data-session]").first.get_attribute("data-session")
    assert session is not None
    return session


def _join_patient(patient: Page, root: Path, width: int) -> None:
    _press(patient, "join")
    expect(patient.locator("#room-name")).to_contain_text("tc-")
    expect(patient.locator("#room-panel")).to_have_attribute("data-role", "patient")
    _capture(patient, root, "patient-room", width)


def _happy_path(  # noqa: PLR0913 - the journey needs its full context
    physician: Page,
    patient: Page,
    staff_url: str,
    patient_url: str,
    staff: dict[str, str],
    data: dict[str, str],
    root: Path,
    width: int,
) -> str:
    """Create, provision, join, start and end one session; return its id."""
    physician.goto(staff_url)
    _capture(physician, root, "staff-empty", width)
    session_id = _create_session(physician, staff_url)
    # Room creation is a committed outbox operation; the worker runs it
    # through the real clinic_app boundary, never inside the request.
    _worker(_room_operation(staff, session_id), "sent", root)
    physician.goto(staff_url)
    expect(physician.locator(f'[data-session="{session_id}"]')).to_have_attribute(
        "data-state", "waiting"
    )
    # Patient enters the waiting room first; the physician joins after.
    patient.goto(patient_url)
    _capture(patient, root, "patient-waiting", width)
    _join_patient(patient, root, width)
    with physician.expect_navigation():
        physician.locator(f'[data-session="{session_id}"] button[value="join"]').click()
    expect(physician.locator("#room-name")).to_contain_text(f"tc-{session_id}")
    expect(physician.locator("#room-panel")).to_have_attribute("data-role", "physician")
    _capture(physician, root, "physician-room", width)
    physician.goto(staff_url)
    with physician.expect_navigation():
        physician.locator(
            f'[data-session="{session_id}"] button[value="start"]'
        ).click()
    expect(physician.locator(f'[data-session="{session_id}"]')).to_have_attribute(
        "data-state", "active"
    )
    with physician.expect_navigation():
        physician.locator(f'[data-session="{session_id}"] button[value="end"]').click()
    expect(physician.locator(f'[data-session="{session_id}"]')).to_have_attribute(
        "data-state", "ended"
    )
    _capture(physician, root, "staff-ended", width)
    # Terminal sessions never reopen: both roles get a conflict, not a room.
    assert (
        post_action(physician, staff_url, {"action": "join", "session_id": session_id})
        == 409
    )
    assert (
        post_action(patient, patient_url, {"action": "join", "session_id": session_id})
        == 409
    )
    return session_id


def _failure_path(  # noqa: PLR0913 - the journey needs its full context
    physician: Page,
    failing_patient: Page,
    staff_url: str,
    patient_url: str,
    staff: dict[str, str],
    failing: dict[str, str],
    base: str,
    day: str,
    root: Path,
    width: int,
) -> None:
    """Provider failure marks the session failed; joins stay closed."""
    _redeem(failing_patient, base, staff["clinic_a"], failing["code"])
    _accept_consent(failing_patient, base)
    _open_encounter(physician, base, staff, day, failing["appointment"])
    physician.goto(staff_url)
    failed_session = _create_session(physician, staff_url)
    _worker(_room_operation(staff, failed_session), "fail", root)
    physician.goto(staff_url)
    expect(physician.locator(f'[data-session="{failed_session}"]')).to_have_attribute(
        "data-state", "failed"
    )
    # The stored failure is persisted by the next mutation attempt, which
    # records the terminal event instead of admitting anyone to a dead room.
    assert (
        post_action(
            physician, staff_url, {"action": "join", "session_id": failed_session}
        )
        == 409
    )
    physician.goto(staff_url)
    expect(
        physician.locator(f'[data-session="{failed_session}"] [data-event="failed"]')
    ).to_contain_text("room_unavailable")
    _capture(physician, root, "staff-failed", width)
    failing_patient.goto(patient_url)
    expect(
        failing_patient.locator(f'[data-session="{failed_session}"]')
    ).to_have_attribute("data-state", "failed")
    assert (
        post_action(
            failing_patient,
            patient_url,
            {"action": "join", "session_id": failed_session},
        )
        == 409
    )


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_teleconsult_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, day = availability_staff, renewal_base_url, DAYS[width]
    root = renewal_artifact_root / "teleconsult"
    root.mkdir(exist_ok=True)
    browser = renewal_page.context.browser
    assert browser is not None
    contexts = [
        browser.new_context(
            locale="pt-BR",
            viewport={"width": width, "height": 900},
            java_script_enabled=False,
        )
        for _ in range(5)
    ]
    physician, patient, denied, failing_patient, admin = [
        context.new_page() for context in contexts
    ]
    errors: list[str] = []
    console: list[str] = []
    for page in (physician, patient, denied, failing_patient, admin):
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                console.append(message.text) if message.type == "error" else None
            ),
        )
    staff_url = f"{base}/teleconsult/clinics/{staff['clinic_a']}/"
    patient_url = f"{base}/patient/teleconsult/"
    data = _seed(staff, day, 12)
    failing = _seed(staff, day, 14)
    try:
        _sign_in_physician(physician, base, staff)
        sign_in_manager(admin, base, staff, seed_manager(staff))
        _publish_consent(admin, f"{base}/clinics/{staff['clinic_a']}/consent/")
        _redeem(patient, base, staff["clinic_a"], data["code"])
        _accept_consent(patient, base)
        # The assigned physician opens the encounter through the real agenda.
        _open_encounter(physician, base, staff, day, data["appointment"])
        session_id = _happy_path(
            physician, patient, staff_url, patient_url, staff, data, root, width
        )
        # A foreign session identifier is denied, never resolved cross-patient.
        other = _seed(staff, day, 16)
        with browser.new_context(
            locale="pt-BR",
            viewport={"width": width, "height": 900},
            java_script_enabled=False,
        ) as other_context:
            other_page = other_context.new_page()
            _redeem(other_page, base, staff["clinic_a"], other["code"])
            other_page.goto(patient_url)
            assert (
                post_action(
                    other_page,
                    patient_url,
                    {"action": "join", "session_id": session_id},
                )
                == 403
            )
            _capture(other_page, root, "cross-patient-denied", width)
        # Staff without the physician role cannot reach the surface at all.
        _sign_in_receptionist(denied, base, staff)
        response = denied.goto(staff_url)
        assert response is not None
        assert response.status == 403
        _capture(denied, root, "reception-denied", width)
        _failure_path(
            physician,
            failing_patient,
            staff_url,
            patient_url,
            staff,
            failing,
            base,
            day,
            root,
            width,
        )
        # Reflow and accessibility scenes reuse the ended session page.
        if width == 375:
            physician.set_viewport_size({"width": 320, "height": 900})
            physician.emulate_media(forced_colors="active", reduced_motion="reduce")
            _capture(physician, root, "reflow-forced-colors", width)
        assert not errors
        unexpected = [
            message
            for message in console
            if "status of 409" not in message and "status of 403" not in message
        ]
        assert not unexpected
        (root / f"accessibility-console-{width}.json").write_text(
            json.dumps(
                {
                    "page_errors": errors,
                    "console_errors": unexpected,
                    "ended_join_status": 409,
                    "cross_patient_status": 403,
                    "reception_status": 403,
                    "failed_join_status": 409,
                    "room_reference": f"tc-{session_id}",
                    "real_provider": "waiting_external",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        for context in contexts:
            context.close()
