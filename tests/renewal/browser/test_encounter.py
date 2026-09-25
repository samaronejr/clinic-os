"""Real clinic_app proof: SOAP saves, stale edits, denial and storage failure."""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect
from psycopg.types.json import Jsonb

from renewal.browser._protected import decrypt
from renewal.browser.engines import new_context
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_questionnaires import _seed as seed_patient

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
FIELDS = ("subjective", "objective", "assessment", "plan")
DAYS = {1280: "2035-06-02", 768: "2035-06-03", 375: "2035-06-04"}


def seed(staff: dict[str, str], day: str) -> dict[str, str]:
    data = seed_patient(staff)
    data.update(appointment=str(uuid4()), specialty=str(uuid4()))
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
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
                f"{day}T12:00:00Z",
                f"{day}T13:00:00Z",
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
                f"{day}T12:00:00Z",
                f"{day}T13:00:00Z",
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.ehr_specialtytemplate "
            "(id,organization_id,clinic_id,key,version,title,prompts,created_at) "
            "VALUES (%s,%s,%s,%s,1,'Clínica geral',%s,now())",
            [
                data["specialty"],
                staff["organization"],
                staff["clinic_a"],
                data["specialty"],
                Jsonb(
                    dict(
                        zip(
                            FIELDS,
                            (
                                "Relato do paciente",
                                "Achados observados",
                                "Avaliação do médico",
                                "Plano registrado pelo médico",
                            ),
                            strict=True,
                        )
                    )
                ),
            ],
        )
    return data


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "encounter"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def press(page: Page, action: str) -> None:
    with page.expect_navigation():
        page.locator(f'button[value="{action}"]').first.click()


def stored(staff: dict[str, str], version: str) -> tuple[int, str]:
    """Return (revision, subjective) with the envelope decrypted in-database."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT revision,content FROM clinic_app.ehr_clinicaldocumentversion "
            "WHERE id=%s",
            [version],
        ).fetchone()
        assert row is not None
        plaintext = decrypt(
            conn,
            "ehr.clinicaldocumentversion.content",
            bytes(row[1]) if row[1] is not None else None,
        )
        subjective = (
            str(json.loads(plaintext)["subjective"]) if plaintext is not None else ""
        )
        return row[0], subjective


def failed_save(
    page: Page, staff: dict[str, str], root: Path, width: int, version: str
) -> None:
    # A task-owned NOT VALID check rejects a real database UPDATE. No production
    # failure flag or mocked response can bypass the real transaction rollback.
    # The check targets ``content``: every draft save writes that column, so the
    # UPDATE itself is rejected and the stored row is provably untouched.
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "ALTER TABLE clinic_app.ehr_clinicaldocumentversion "
            "ADD CONSTRAINT task21_failure "
            "CHECK (content IS NULL) NOT VALID"
        )
    try:
        page.locator("#id_subjective").fill("Falha sintética")
        with page.expect_response(lambda r: r.request.method == "POST") as response:
            press(page, "save")
        assert response.value.status == 503
        expect(page.locator("#save-state")).to_have_attribute("data-state", "unsaved")
        expect(page.locator("#id_subjective")).to_have_value("Falha sintética")
        assert stored(staff, version) == (2, "Relato sintético salvo")
        capture(page, root, "failed-save-retained", width)
    finally:
        with psycopg.connect(staff["dsn"]) as conn:
            conn.execute(
                "ALTER TABLE clinic_app.ehr_clinicaldocumentversion "
                "DROP CONSTRAINT task21_failure"
            )


def edit_and_reload(page: Page, staff: dict[str, str], root: Path, width: int) -> str:
    version = page.locator("[data-version]").get_attribute("data-version")
    assert version is not None
    expect(page.locator("[data-template-version]")).to_have_attribute(
        "data-template-version", "1"
    )
    capture(page, root, "draft", width)
    second = page.context.new_page()
    second.goto(page.url)
    for field in FIELDS:
        page.locator(f"#id_{field}").fill(
            "Relato sintético salvo"
            if field == "subjective"
            else f"Registro sintético {field}"
        )
    expect(page.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    capture(page, root, "unsaved", width)
    # Pause native submission exactly once after the product's submit listener.
    page.evaluate("""document.querySelector('#soap-form').addEventListener(
        'submit', event => event.preventDefault(), {once: true})""")
    page.locator('button[value="save"]').click()
    expect(page.locator("#save-state")).to_have_attribute("data-state", "saving")
    capture(page, root, "saving", width)
    press(page, "save")
    expect(page.locator("#save-state")).to_have_attribute("data-state", "saved")
    capture(page, root, "saved", width)
    page.reload()
    expect(page.locator("#id_subjective")).to_have_value("Relato sintético salvo")
    expect(page.locator("[data-version]")).to_have_attribute("data-version", version)
    assert stored(staff, version) == (2, "Relato sintético salvo")
    capture(page, root, "reloaded", width)
    second.locator("#id_subjective").fill("Edição antiga não salva")
    with second.expect_response(lambda r: r.request.method == "POST") as stale:
        press(second, "save")
    assert stale.value.status == 409
    expect(second.locator("#id_subjective")).to_have_value("Edição antiga não salva")
    expect(second.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    capture(second, root, "stale-retained", width)
    second.close()
    return version


def check_reflow(page: Page, root: Path) -> None:
    page.set_viewport_size({"width": 320, "height": 900})
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    capture(page, root, "forced-colors-reflow", 320)
    page.emulate_media(forced_colors="none")
    page.locator("#id_subjective").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#id_objective")).to_be_focused()
    capture(page, root, "keyboard-focus", 320)
    browser = page.context.browser
    assert browser is not None
    # Same browser-zoom metric contract as the existing availability suite:
    # 640 CSS px at DPR 2 is a 1280 physical-pixel window with 200% text.
    native = new_context(
        browser,
        locale="pt-BR",
        java_script_enabled=False,
        storage_state=page.context.storage_state(),
        viewport={"width": 640, "height": 450},
        device_scale_factor=2,
    )
    try:
        fallback = native.new_page()
        fallback.goto(page.url)
        assert fallback.evaluate("devicePixelRatio") == 2
        assert fallback.evaluate("innerWidth") == 640
        fallback.locator("#id_objective").fill("L" * 20000)
        press(fallback, "save")
        fallback.reload()
        expect(fallback.locator("#id_objective")).to_have_value("L" * 20000)
        expect(fallback.locator("[data-revision]")).to_have_attribute(
            "data-revision", "3"
        )
        capture(fallback, root, "native-long-zoom-200", 640)
        target = fallback.locator('button[value="save"]').bounding_box()
        assert target is not None
        assert target["height"] >= 44
    finally:
        native.close()


def reception_denial(
    page: Page, staff: dict[str, str], base: str, root: Path, width: int
) -> None:
    browser = page.context.browser
    assert browser is not None
    denied_context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    try:
        denied = denied_context.new_page()
        _sign_in_receptionist(denied, base, staff)
        response = denied.goto(page.url)
        assert response is not None
        assert response.status == 403
        assert "Relato sintético salvo" not in denied.content()
        capture(denied, root, "reception-denied", width)
    finally:
        denied_context.close()


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_encounter_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    data = seed(staff, DAYS[width])
    base = renewal_base_url
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/encounter/"
    root = renewal_artifact_root
    try:
        _sign_in_physician(page, base, staff)
        page.goto(url)
        expect(page.locator("#encounter-title")).to_be_visible()
        capture(page, root, "empty", width)
        page.goto(
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{DAYS[width]}/1/"
        )
        expect(page.locator('button[value="open"]')).to_be_visible()
        capture(page, root, "appointment", width)
        # Both authenticated starts use real middleware and the same appointment.
        statuses = page.evaluate(
            """async (url) => {
          const form = document.querySelector('button[value="open"]').form;
          const send = () => {
            const body = new FormData(form); body.set('action','open');
            return fetch(url, {method:'POST', body}).then(r => r.status); };
          return Promise.all([send(), send()]);
        }""",
            url,
        )
        assert statuses == [200, 200]
        press(page, "open")
        capture(page, root, "choose-template", width)
        page.locator("#template-id").select_option(data["specialty"])
        press(page, "template")
        version = edit_and_reload(page, staff, root, width)
        failed_save(page, staff, root, width, version)
        page.goto(url)
        expect(page.locator("#id_subjective")).to_have_value("Relato sintético salvo")
        with psycopg.connect(staff["dsn"]) as conn:
            conn.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [staff["organization"]],
            )
            assert conn.execute(
                "SELECT count(*) FROM clinic_app.ehr_encounter WHERE appointment_id=%s",
                [data["appointment"]],
            ).fetchone() == (1,)
        if width == 375:
            check_reflow(page, root)
        reception_denial(page, staff, base, root, width)
        assert not errors
        (root / "encounter" / f"report-{width}.json").write_text(
            json.dumps(
                {
                    "width": width,
                    "page_errors": errors,
                    "race_encounters": 1,
                    "save_reload_revision": 2,
                    "stale_http": 409,
                    "failure_http": 503,
                    "reflow": True,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()
