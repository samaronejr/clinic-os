"""Real patient draft/resume/submit and clinical inspection through clinic_app."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect
from psycopg.types.json import Jsonb

from renewal.browser._protected import encrypt
from renewal.browser.engines import full_page_screenshot, new_context
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_patient_access import _redeem, _width

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from playwright.sync_api import Page

QUESTIONS = [
    {
        "id": "q_text",
        "label": "Informações para a consulta",
        "type": "text",
        "required": True,
        "max_length": 40,
        "options": [],
    },
    {
        "id": "q_choice",
        "label": "Preferência de contato",
        "type": "selection",
        "required": True,
        "max_length": 20,
        "options": ["Telefone", "Mensagem"],
    },
    {
        "id": "q_bool",
        "label": "Deseja conversar?",
        "type": "boolean",
        "required": True,
        "max_length": 5,
        "options": [],
    },
]
PRIVATE = "Informação sintética privada"
LONG_ANSWER = "A" * 4000
LONG_QUESTIONS: list[dict[str, object]] = [
    {
        "id": f"q_long_{index}",
        "label": "L" * 200 if index == 0 else f"Pergunta sintética {index + 1}",
        "type": "text",
        "required": True,
        "max_length": 4000,
        "options": [],
    }
    for index in range(40)
]
__all__ = ("availability_staff",)


def _seed(
    staff: dict[str, str],
    *,
    questions: list[dict[str, object]] | None = None,
    title: str = "Pré-consulta sintética",
) -> dict[str, str]:
    values = {
        key: str(uuid4())
        for key in ("patient", "enrollment", "template", "response", "grant")
    }
    values["code"] = secrets.token_urlsafe(32)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patient "
            "(id,organization_id,full_name,birth_date,created_at) "
            "VALUES (%s,%s,%s,%s,now())",
            [
                values["patient"],
                staff["organization"],
                encrypt(
                    conn,
                    "intake.patient.full_name",
                    "Paciente Sintético Questionário".encode(),
                ),
                encrypt(conn, "intake.patient.birth_date", b"1990-01-01"),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientclinicenrollment "
            "(id,organization_id,clinic_id,patient_id,idempotency_key,"
            "create_fingerprint,created_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
            [
                values["enrollment"],
                staff["organization"],
                staff["clinic_a"],
                values["patient"],
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_questionnairetemplate "
            "(id,organization_id,clinic_id,key,version,title,questions,created_at) "
            "VALUES (%s,%s,%s,%s,1,%s,%s,now())",
            [
                values["template"],
                staff["organization"],
                staff["clinic_a"],
                values["template"],
                title,
                Jsonb(QUESTIONS if questions is None else questions),
            ],
        )
        # The response guard admits only an empty initial draft: answers and
        # reopen_reason stay NULL until the patient session writes envelopes.
        conn.execute(
            "INSERT INTO clinic_app.intake_questionnaireresponse "
            "(id,organization_id,clinic_id,patient_id,enrollment_id,template_id,"
            "state,revision,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'draft',1,now(),now())",
            [
                values["response"],
                staff["organization"],
                staff["clinic_a"],
                values["patient"],
                values["enrollment"],
                values["template"],
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientaccessgrant "
            "(id,organization_id,clinic_id,patient_id,enrollment_id,issued_by_id,"
            "issued_by_label,secret_hash,operations,expires_at,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'Synthetic',%s,"
            "ARRAY['enrollment_view','questionnaires'],"
            "now()+interval '24 hours',now())",
            [
                values["grant"],
                staff["organization"],
                staff["clinic_a"],
                values["patient"],
                values["enrollment"],
                staff["receptionist_id"],
                hashlib.sha256(values["code"].encode()).digest(),
            ],
        )
    return values


def _capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "questionnaires"
    folder.mkdir(exist_ok=True, mode=0o700)
    full_page_screenshot(page, folder / f"{state}-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _press(page: Page, action: str) -> None:
    with page.expect_navigation():
        page.locator(f'button[value="{action}"]').first.click()


def _other_patient_denial(
    page: Page,
    base_url: str,
    root: Path,
    staff: dict[str, str],
    data: dict[str, str],
) -> None:
    other = _seed(staff)
    _redeem(page, base_url, staff["clinic_a"], other["code"])
    page.goto(f"{base_url}/patient/questionnaires/")
    page.locator('input[name="response_id"]').evaluate(
        "(el, value) => el.value = value", data["response"]
    )
    with page.expect_response(lambda r: r.request.method == "POST") as denied:
        _press(page, "open")
    assert denied.value.status == 403
    assert PRIVATE not in page.content()
    _capture(page, root, "other-patient-denied", _width(page))


def _staff_journey(
    page: Page,
    base_url: str,
    root: Path,
    staff: dict[str, str],
    data: dict[str, str],
) -> None:
    width = _width(page)
    _sign_in_receptionist(page, base_url, staff)
    staff_url = f"{base_url}/intake/clinics/{staff['clinic_a']}/questionnaires/"
    page.goto(f"{base_url}/intake/clinics/{staff['clinic_a']}/patients/")
    page.locator("#id_q").fill("Paciente Sintético Questionário")
    with page.expect_response(lambda r: r.request.method == "POST"):
        page.locator("#patient-search-form button[type=submit]").click()
    row = page.locator("tr").filter(
        has=page.locator(f'input[value="{data["enrollment"]}"]')
    )
    expect(row.get_by_role("button", name="Questionários")).to_be_visible()
    _capture(page, root, "reception-patient-search", width)
    with page.expect_navigation():
        row.get_by_role("button", name="Questionários").click()
    expect(page.locator('[data-state="submitted"]')).to_be_visible()
    assert PRIVATE not in page.content()
    _capture(page, root, "reception-status", width)
    with page.expect_response(lambda r: r.request.method == "POST") as denied:
        _press(page, "inspect")
    assert denied.value.status == 403
    assert PRIVATE not in page.content()
    _capture(page, root, "reception-denied", width)
    _sign_in_physician(page, base_url, staff)
    page.goto(staff_url)
    page.locator("#enrollment-id").fill(data["enrollment"])
    _press(page, "status")
    _press(page, "inspect")
    expect(page.locator("main")).to_contain_text(PRIVATE)
    expect(page.locator("[data-template-version]")).to_have_attribute(
        "data-template-version", "1"
    )
    _capture(page, root, "clinical-inspection", width)
    page.locator("#reopen-reason").fill("Correção solicitada pelo paciente")
    _press(page, "reopen")
    expect(page.locator("[data-template-version]")).to_have_attribute(
        "data-state", "draft"
    )
    _capture(page, root, "clinical-reopened", width)


def _publish_version_two(staff: dict[str, str], data: dict[str, str]) -> None:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_questionnairetemplate "
            "(id,organization_id,clinic_id,key,version,title,questions,created_at) "
            "VALUES (%s,%s,%s,%s,2,'Configuração nova',%s,now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                data["template"],
                Jsonb(QUESTIONS),
            ],
        )


def _publish_and_resume(
    page: Page,
    base_url: str,
    root: Path,
    staff: dict[str, str],
    data: dict[str, str],
) -> None:
    _publish_version_two(staff, data)
    page.goto(f"{base_url}/patient/questionnaires/")
    _press(page, "open")
    expect(page.locator("#id_q_text")).to_have_value(PRIVATE)
    expect(page.locator("[data-template-version]")).to_have_attribute(
        "data-template-version", "1"
    )
    assert "Configuração nova" not in page.content()
    _capture(page, root, "resumed-version-1", _width(page))


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_versioned_patient_and_clinical_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    data = _seed(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text)
            if message.type == "error" and "403" not in message.text
            else None
        ),
    )
    try:
        _redeem(page, renewal_base_url, staff["clinic_a"], data["code"])
        page.get_by_role("link", name="Questionários antes da consulta").click()
        _capture(page, renewal_artifact_root, "assigned", width)
        _press(page, "open")
        _capture(page, renewal_artifact_root, "empty", width)
        _press(page, "submit")
        expect(page.get_by_role("alert")).to_be_visible()
        _capture(page, renewal_artifact_root, "required-error", width)
        page.locator("#id_q_text").fill(PRIVATE)
        _press(page, "save")
        expect(page.locator('[name="revision"]')).to_have_value("2")
        _capture(page, renewal_artifact_root, "draft-saved", width)
        _publish_and_resume(page, renewal_base_url, renewal_artifact_root, staff, data)
        page.locator("#id_q_text").evaluate(
            "element => element.removeAttribute('maxlength')"
        )
        page.locator("#id_q_text").fill("x" * 41)
        _press(page, "submit")
        expect(page.get_by_role("alert")).to_be_visible()
        _capture(page, renewal_artifact_root, "overlong-error", width)
        page.locator("#id_q_text").fill(PRIVATE)
        page.locator("#id_q_choice").select_option("Mensagem")
        page.locator("#id_q_bool").select_option("False")
        # Native keyboard submission, no JS-only form transport.
        page.locator('button[value="submit"]').focus()
        with page.expect_navigation():
            page.keyboard.press("Enter")
        expect(page.locator("[data-template-version]")).to_have_attribute(
            "data-state", "submitted"
        )
        _capture(page, renewal_artifact_root, "submitted", width)
        assert data["response"] not in page.url
        _other_patient_denial(
            page, renewal_base_url, renewal_artifact_root, staff, data
        )
        _staff_journey(page, renewal_base_url, renewal_artifact_root, staff, data)
        page.set_viewport_size({"width": 320, "height": 900})
        page.emulate_media(forced_colors="active")
        _capture(page, renewal_artifact_root, "reflow-forced-colors", width)
        assert not errors
        report = {
            "width": width,
            "console_errors": errors,
            "version_retained": 1,
            "native_keyboard_submit": True,
            "other_patient_http": 403,
            "reception_http": 403,
            "reflow": 320,
            "reduced_motion": True,
            "forced_colors": True,
        }
        (
            renewal_artifact_root
            / "questionnaires"
            / f"accessibility-console-{width}.json"
        ).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        context.close()


def _stale_editor_conflict(
    page: Page,
    other: Page,
    base_url: str,
    capture: Callable[[Page, str], None],
) -> str:
    saved_answer = "Resposta salva na outra aba"
    rejected_answer = "Edição antiga não deve substituir a resposta salva"
    other.goto(f"{base_url}/patient/questionnaires/")
    _press(other, "open")
    expect(other.locator("[data-template-version]")).to_have_attribute(
        "data-template-version", "1"
    )
    other.locator("#id_q_long_39").fill(saved_answer)
    _press(other, "save")
    expect(other.locator('[name="revision"]')).to_have_value("3")
    page.locator("#id_q_long_39").fill(rejected_answer)
    _press(page, "save")
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.locator('[name="revision"]')).to_have_value("2")
    expect(page.locator("#id_q_long_39")).to_have_value(rejected_answer)
    expect(page.locator("[data-template-version]")).to_have_attribute(
        "data-template-version", "1"
    )
    capture(page, "stale-editor-conflict")

    # Recover through the rendered open action, not a DB or DOM reset.
    _press(page, "open")
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.locator('[name="revision"]')).to_have_value("3")
    expect(page.locator("#id_q_long_39")).to_have_value(saved_answer)
    expect(page.locator("#id_q_long_0")).to_have_value(LONG_ANSWER)
    page.locator('button[value="submit"]').focus()
    expect(page.locator('button[value="submit"]')).to_be_focused()
    with page.expect_navigation():
        page.keyboard.press("Enter")
    expect(page.locator("[data-template-version]")).to_have_attribute(
        "data-state", "submitted"
    )
    capture(page, "stale-editor-recovered-submitted")
    return saved_answer


@pytest.mark.parametrize(
    "scene", [(width, zoom) for zoom in (1, 2) for width in (375, 768, 1280)]
)
def test_long_content_and_stale_editor_matrix(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    scene: tuple[int, int],
) -> None:
    """Real concurrent editors, retained schema, and boundary-valid content.

    Match the sibling suites' browser-zoom emulation: halve the CSS viewport
    and double device scale, not CSS transforms or pinch magnification. At
    375px/200%, round down to 187 CSS px (374 device px), conservatively.
    """
    width, zoom = scene
    staff = availability_staff
    data = _seed(staff, questions=LONG_QUESTIONS, title="T" * 160)
    browser = renewal_page.context.browser
    assert browser is not None
    context = new_context(
        browser,
        locale="pt-BR",
        viewport={"width": width // zoom, "height": 900 // zoom},
        device_scale_factor=zoom,
        reduced_motion="reduce",
    )
    errors: list[str] = []
    captures: list[dict[str, object]] = []
    suffix = f"zoom-{zoom * 100}"

    def capture(surface: Page, state: str) -> None:
        metrics: dict[str, int] = surface.evaluate(
            """() => ({
                css_viewport_width: innerWidth,
                device_pixel_ratio: devicePixelRatio,
                scroll_width: document.documentElement.scrollWidth,
                unlabeled_controls: [...document.querySelectorAll(
                    'main textarea, main select, main input:not([type=hidden])'
                )].filter(el => !el.labels.length).length
            })"""
        )
        assert metrics["css_viewport_width"] == width // zoom
        assert metrics["device_pixel_ratio"] == zoom
        assert metrics["unlabeled_controls"] == 0
        _capture(surface, renewal_artifact_root, f"{state}-{suffix}", width)
        captures.append({"path": f"{state}-{suffix}-{width}.png", **metrics})

    def watch(surface: Page) -> None:
        surface.set_default_timeout(20_000)
        surface.on("pageerror", lambda error: errors.append(str(error)))
        surface.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type == "error" else None
            ),
        )

    try:
        page = context.new_page()
        watch(page)
        _redeem(page, renewal_base_url, staff["clinic_a"], data["code"])
        page.goto(f"{renewal_base_url}/patient/questionnaires/")
        _press(page, "open")
        for index in range(40):
            page.locator(f"#id_q_long_{index}").fill(
                LONG_ANSWER if index == 0 else f"Resposta sintética {index + 1}"
            )
        _press(page, "save")
        expect(page.locator('[name="revision"]')).to_have_value("2")
        expect(page.locator("#id_q_long_0")).to_have_value(LONG_ANSWER)
        capture(page, "long-content-draft")

        # Publish a genuinely different three-question schema while v1 is open.
        # Publication alone must NOT invalidate this immutable assignment.
        _publish_version_two(staff, data)
        other = context.new_page()
        watch(other)
        saved_answer = _stale_editor_conflict(page, other, renewal_base_url, capture)

        _sign_in_physician(page, renewal_base_url, staff)
        page.goto(
            f"{renewal_base_url}/intake/clinics/{staff['clinic_a']}/questionnaires/"
        )
        page.locator("#enrollment-id").fill(data["enrollment"])
        _press(page, "status")
        _press(page, "inspect")
        expect(page.locator("[data-template-version]")).to_have_attribute(
            "data-template-version", "1"
        )
        expect(page.locator("dd")).to_have_text(
            [LONG_ANSWER]
            + [f"Resposta sintética {index + 1}" for index in range(1, 39)]
            + [saved_answer]
        )
        capture(page, "long-content-clinical")
        assert not errors, errors
        report = {
            "window_width": width,
            "zoom_percent": zoom * 100,
            "zoom_method": "CSS viewport divided by zoom; device scale equals zoom",
            "question_count": 40,
            "unbroken_title_length": 160,
            "unbroken_label_length": 200,
            "unbroken_answer_length": len(LONG_ANSWER),
            "published_version": 2,
            "retained_version": 1,
            "stale_revision": 2,
            "recovered_revision": 3,
            "keyboard_submission": True,
            "concurrent_answer_preserved": True,
            "console_errors": errors,
            "captures": captures,
        }
        (
            renewal_artifact_root
            / "questionnaires"
            / f"matrix-accessibility-console-{suffix}-{width}.json"
        ).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        context.close()


def test_native_questionnaire_without_javascript(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    data = _seed(availability_staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False,
        locale="pt-BR",
        viewport={"width": 375, "height": 900},
    )
    try:
        page = context.new_page()
        _redeem(page, renewal_base_url, availability_staff["clinic_a"], data["code"])
        page.get_by_role("link", name="Questionários antes da consulta").click()
        _press(page, "open")
        page.locator("#id_q_text").fill(PRIVATE)
        _press(page, "save")
        expect(page.locator('[name="revision"]')).to_have_value("2")
        page.goto(f"{renewal_base_url}/patient/questionnaires/")
        _press(page, "open")
        expect(page.locator("#id_q_text")).to_have_value(PRIVATE)
        page.locator("#id_q_choice").select_option("Telefone")
        page.locator("#id_q_bool").select_option("True")
        _press(page, "submit")
        expect(page.locator("[data-template-version]")).to_have_attribute(
            "data-state", "submitted"
        )
        _capture(page, renewal_artifact_root, "native-no-javascript", 375)
    finally:
        context.close()
