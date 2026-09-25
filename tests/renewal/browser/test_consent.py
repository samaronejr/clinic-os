"""Real patient and staff consent surfaces, clinic_app serving and native forms."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser._protected import decrypt, encrypt
from renewal.browser.test_availability import availability_staff
from renewal.browser.test_encounter import press
from renewal.browser.test_patient_access import _redeem, _watch_errors
from renewal.browser.test_retention import post_action, seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
TEXT = (
    "Consentimento sintético para teleconsulta.\n\n"
    "A consulta será realizada por vídeo, sem gravação ou transcrição. "
    "Se houver dificuldade técnica, entre em contato com a clínica. "
    "Você pode revogar esta autorização para impedir novos usos. "
    "Os registros clínicos anteriores serão preservados. "
    "Este texto não autoriza marketing ou mensagens.\n\n"
    + "Informação complementar sintética para leitura: "
    * 12
)


def seed_patient(staff: dict[str, str]) -> dict[str, str]:
    """Create only synthetic invitation fixtures; redemption uses the real portal."""
    data = {key: str(uuid4()) for key in ("patient", "enrollment", "grant")}
    data["code"] = secrets.token_urlsafe(32)
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
                    "Paciente Sintético Consentimento".encode(),
                ),
                encrypt(conn, "intake.patient.birth_date", b"1990-01-01"),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientclinicenrollment "
            "(id,organization_id,clinic_id,patient_id,idempotency_key,"
            "create_fingerprint,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,now())",
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
            "ARRAY['enrollment_view','consent'],now()+interval '24 hours',now())",
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
    return data


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "consent"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def publish(page: Page, url: str, text: str, purpose: str | None = None) -> None:
    page.goto(url)
    if purpose is not None:
        page.locator("#id_purpose").select_option(purpose)
    page.locator("#id_text").fill(text)
    press(page, "publish")
    expect(page.locator('[role="status"]')).to_be_visible()


def read_text(page: Page, purpose_label: str) -> None:
    """Open the displayed text for one purpose from the list of versions."""
    row = page.locator("form", has_text=purpose_label)
    with page.expect_navigation():
        row.locator('button[value="read"]').click()


def refuse_current(page: Page, root: Path, width: int) -> None:
    """Record one refusal for the currently displayed version."""
    with page.expect_navigation():
        page.locator('button[value="refuse"]').click()
    refusal = page.locator("[data-refusal]")
    expect(refusal).to_be_visible()
    capture(page, root, "refusal-recorded", width)


@dataclass(frozen=True)
class Flow:
    """The shared pages and identifiers for the consent surfaces."""

    admin: Page
    patient: Page
    staff_url: str
    base: str
    enrollment_id: str
    root: Path
    width: int


def staff_receipt_view(flow: Flow) -> None:
    """Show the enrollment's receipts and refusal rows on the staff surface."""
    flow.admin.goto(flow.staff_url)
    flow.admin.locator("#enrollment-id").fill(flow.enrollment_id)
    press(flow.admin, "receipts")


def taxonomy_flow(flow: Flow) -> None:
    """Publish recording consent and an AI notice; accept one, refuse one."""
    publish(
        flow.admin,
        flow.staff_url,
        "Texto sintético de gravação da consulta.",
        purpose="consultation_recording",
    )
    publish(
        flow.admin,
        flow.staff_url,
        "Texto sintético de assistência por IA.",
        purpose="ai_assistance",
    )
    flow.admin.goto(flow.staff_url)
    flow.admin.locator("#id_notice-topic").select_option("ai_use")
    flow.admin.locator("#id_notice-text").fill(
        "Aviso sintético sobre uso de IA no atendimento."
    )
    press(flow.admin, "publish_notice")
    expect(flow.admin.locator('[role="status"]')).to_be_visible()
    flow.patient.goto(f"{flow.base}/patient/consent/")
    notice_article = flow.patient.locator("[data-notice]")
    expect(notice_article).to_be_visible()
    notice_article.locator("summary").click()
    expect(notice_article.locator(".consent-text")).to_contain_text("uso de IA")
    capture(flow.patient, flow.root, "notice-listed", flow.width)
    read_text(flow.patient, "Gravação da consulta")
    flow.patient.locator("#id_accepted").check()
    press(flow.patient, "accept")
    recording = flow.patient.locator("[data-receipt][data-state='accepted']")
    expect(recording.first).to_be_visible()
    capture(flow.patient, flow.root, "recording-accepted", flow.width)
    read_text(flow.patient, "Assistência por IA")
    refuse_current(flow.patient, flow.root, flow.width)
    expect(flow.patient.locator("[data-refusal]")).to_contain_text("Assistência por IA")
    staff_receipt_view(flow)
    expect(flow.admin.locator("[data-refusal]")).to_have_count(1)
    capture(flow.admin, flow.root, "staff-refusal", flow.width)


def accept_revoke(patient: Page, root: Path, width: int) -> str:
    with patient.expect_response(lambda r: r.request.method == "POST") as missing:
        press(patient, "accept")
    assert missing.value.status == 400
    capture(patient, root, "explicit-choice-required", width)
    press(patient, "read")
    patient.locator("#id_accepted").focus()
    patient.keyboard.press("Space")
    expect(patient.locator("#id_accepted")).to_be_checked()
    patient.keyboard.press("Tab")
    button = patient.locator('button[value="accept"]')
    expect(button).to_be_focused()
    assert button.evaluate("e => e.getBoundingClientRect().height >= 44")
    assert button.evaluate("e => getComputedStyle(e).outlineStyle !== 'none'")
    capture(patient, root, "keyboard-focus", width)
    with patient.expect_navigation():
        patient.keyboard.press("Enter")
    receipt = patient.locator("[data-receipt]")
    expect(receipt).to_have_attribute("data-state", "accepted")
    receipt.locator("summary").click()
    assert (
        receipt.locator(".accepted-text").text_content()
        == TEXT + "\nNova versão sintética."
    )
    capture(patient, root, "accepted-exact-receipt", width)
    receipt_id = receipt.get_attribute("data-receipt")
    assert receipt_id is not None
    assert (
        post_action(
            patient, patient.url, {"action": "delete", "acceptance_id": receipt_id}
        )
        == 403
    )
    press(patient, "revoke")
    expect(patient.locator("[data-receipt]")).to_have_attribute("data-state", "revoked")
    patient.locator("summary").click()
    capture(patient, root, "revoked-retained-receipt", width)
    patient.reload()
    expect(patient.locator("[data-receipt]")).to_have_attribute("data-state", "revoked")
    return receipt_id


def verify_stored(staff: dict[str, str], receipt_id: str) -> None:
    # Native HTML form submission encodes newlines as CRLF. Preserve and
    # verify those exact received UTF-8 bytes, not DOM-normalized LF text.
    expected_text = (TEXT + "\nNova versão sintética.").replace("\n", "\r\n")
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        rows = conn.execute(
            "SELECT t.text,t.digest,a.authority,r.id IS NOT NULL "
            "FROM clinic_app.consent_consentacceptance a "
            "JOIN clinic_app.consent_consenttext t ON t.id=a.text_id "
            "LEFT JOIN clinic_app.consent_consentrevocation r ON r.acceptance_id=a.id "
            "WHERE a.id=%s",
            [receipt_id],
        ).fetchall()
        assert len(rows) == 1
        text, digest, authority, revoked = rows[0]
        plaintext = decrypt(conn, "consent.consenttext.text", bytes(text))
        assert (
            plaintext.decode("utf-8") if plaintext is not None else None,
            digest,
            authority,
            revoked,
        ) == (
            expected_text,
            hashlib.sha256(expected_text.encode()).hexdigest(),
            "patient_explicit_action",
            True,
        )


def accessibility_scenes(patient: Page, root: Path) -> None:
    patient.set_viewport_size({"width": 320, "height": 900})
    patient.locator("[data-receipt] summary").click()
    capture(patient, root, "reflow", 320)
    patient.emulate_media(forced_colors="active", reduced_motion="reduce")
    capture(patient, root, "forced-colors-reduced-motion", 320)
    patient.emulate_media(forced_colors="none")
    cdp = patient.context.new_cdp_session(patient)
    cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 640,
            "height": 450,
            "deviceScaleFactor": 2,
            "mobile": False,
        },
    )
    assert patient.evaluate("devicePixelRatio === 2 && innerWidth === 640")
    capture(patient, root, "zoom-200-layout", 640)
    cdp.detach()


def console_report(
    root: Path, width: int, errors: list[str], patient_errors: list[str]
) -> None:
    expected_messages = {
        "Failed to load resource: the server responded with a status of 409 (Conflict)",
        "Failed to load resource: the server responded with a status "
        "of 400 (Bad Request)",
    }
    expected_conflicts = [
        error for error in patient_errors if error in expected_messages
    ]
    unexpected = errors + [
        error for error in patient_errors if error not in expected_messages
    ]
    assert not unexpected
    (root / "consent" / f"accessibility-console-{width}.json").write_text(
        json.dumps(
            {
                "console_errors": unexpected,
                "expected_http_conflicts": expected_conflicts,
                "native_no_javascript": True,
                "keyboard_acceptance": True,
                "reflow": True,
                "receipt_digest_verified": True,
                "stale_text_status": 409,
                "cross_patient_status": 403,
                "delete_status": 403,
                "recording_accepted": True,
                "ai_assistance_refused": True,
                "notice_published": True,
            },
            indent=2,
        )
        + "\n"
    )


def replay_denied(page: Page, token: str, root: Path, width: int) -> None:
    press(page, "read")
    page.locator("#id_offer").evaluate("(e, token) => { e.value = token; }", token)
    page.locator("#id_accepted").check()
    with page.expect_response(lambda r: r.request.method == "POST") as response:
        press(page, "accept")
    assert response.value.status == 403
    capture(page, root, "cross-patient-denied", width)


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_accept_revoke_receipt_and_replay_denials(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    staff = availability_staff
    base = renewal_base_url
    root = renewal_artifact_root
    data = seed_patient(staff)
    other = seed_patient(staff)
    with (
        browser.new_context(
            viewport={"width": width, "height": 900},
            locale="pt-BR",
            java_script_enabled=False,
        ) as staff_context,
        browser.new_context(
            viewport={"width": width, "height": 900},
            locale="pt-BR",
            java_script_enabled=False,
        ) as patient_context,
    ):
        admin = staff_context.new_page()
        patient = patient_context.new_page()
        errors = _watch_errors(admin)
        patient_errors = _watch_errors(patient)
        sign_in_manager(admin, base, staff, seed_manager(staff))
        staff_url = f"{base}/clinics/{staff['clinic_a']}/consent/"
        publish(admin, staff_url, TEXT)
        _redeem(patient, base, staff["clinic_a"], data["code"])
        patient.get_by_role("link", name="Seus consentimentos").click()
        capture(patient, root, "default-empty-receipts", width)
        press(patient, "read")
        expect(patient.locator("#id_accepted")).not_to_be_checked()
        assert patient.locator("#consent-text").text_content() == TEXT
        capture(patient, root, "read-unchecked", width)
        token = patient.locator("#id_offer").input_value()
        # Exact stale editor: publication happens after this patient read.
        publish(admin, staff_url, TEXT + "\nNova versão sintética.")
        patient.locator("#id_accepted").check()
        with patient.expect_response(lambda r: r.request.method == "POST") as response:
            press(patient, "accept")
        assert response.value.status == 409
        capture(patient, root, "stale-text-conflict", width)
        assert (
            post_action(admin, staff_url, {"action": "accept", "offer": token}) == 403
        )
        # A token signed for teleconsultation cannot be replayed as another
        # purpose; the service denies identically to a forged token.
        assert (
            post_action(
                patient,
                patient.url,
                {
                    "action": "accept",
                    "offer": token,
                    "purpose": "marketing",
                    "accepted": "on",
                },
            )
            == 403
        )
        press(patient, "read")
        token = patient.locator("#id_offer").input_value()
        with browser.new_context(
            java_script_enabled=False, viewport={"width": width, "height": 900}
        ) as other_context:
            other_page = other_context.new_page()
            _redeem(other_page, base, staff["clinic_a"], other["code"])
            other_page.goto(f"{base}/patient/consent/")
            replay_denied(other_page, token, root, width)
        receipt_id = accept_revoke(patient, root, width)
        flow = Flow(
            admin=admin,
            patient=patient,
            staff_url=staff_url,
            base=base,
            enrollment_id=data["enrollment"],
            root=root,
            width=width,
        )
        staff_receipt_view(flow)
        expect(admin.locator("[data-receipt]")).to_have_attribute(
            "data-state", "revoked"
        )
        admin.locator("summary").last.click()
        assert (
            admin.locator(".accepted-text").text_content()
            == TEXT + "\nNova versão sintética."
        )
        capture(admin, root, "staff-receipt", width)
        # Purpose taxonomy: recording consent and an AI-use notice, then the
        # patient accepts recording and refuses AI assistance in the portal.
        taxonomy_flow(flow)
        # Additional reflow/accessibility scenes share the exact same receipt.
        if width == 375:
            accessibility_scenes(patient, root)
        verify_stored(staff, receipt_id)
        console_report(root, width, errors, patient_errors)
