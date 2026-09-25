"""Synthetic design-partner rehearsal: one clinic day across every persona.

Reception, a clinic administrator, the assigned physician and the invited
patient drive the real runtime pages, served as ``clinic_app`` with the real
middleware, CSRF, TOTP/step-up and RLS stack, through one continuous journey:
clinic configuration, availability, registration, booking, verified contact,
invitation, questionnaire assignment and answer, consent, encounter notes,
video, finalization, a non-controlled signed document (sign, verify, release,
deliver, download, revoke), PIX instructions and receipt, and the controlled
record export. Only fixture identities (staff users, the owner-provisioned
synthetic physician profile) are seeded; every clinical and payment record is
created through a screen.

Adapters are labelled in ``ADAPTERS`` and written into every run report: no
provider is a real sandbox, because the capability register approves none. The
journey asserts the screens say so (``sem validade``, ``não pagável``,
``Ensaio sintético``) and never claim a real integration.

Evidence grid: the day runs at 1280 px (desktop) and 375 px (mobile, with the
320 px reflow, forced colors and reduced motion scenes); the recovery day runs
at 768 px and interrupts booking, note save, video, signature and payment
after the server committed, retries, and then uses a revoked patient session
and a wrong-clinic staff session. Every wait subscribes to a navigation, a
response, a request failure or a DOM state; nothing sleeps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
import rfc8785
from django_otp.oath import TOTP
from playwright.sync_api import expect, sync_playwright

from renewal.browser._page_wait import wait_for_js
from renewal.browser.test_availability import (
    SETTLED_JS,
    _sign_in_physician,
    _sign_in_receptionist,
    _submit_expecting_success,
    availability_staff,
)
from renewal.browser.test_availability import _fill as fill_availability
from renewal.browser.test_billing import (
    AMOUNT,
    AMOUNT_TEXT,
    CODE_PREFIX,
    confirm_settlement,
    expect_state,
    invoice_id_of,
    qr_renders,
)
from renewal.browser.test_document_verification import (
    END,
    MARKER,
    SIGNATURE_HEADER,
    SYNTHETIC_SECRET,
    _document_row,
    _operation_row,
)
from renewal.browser.test_document_verification import _worker as delivery_worker
from renewal.browser.test_encounter import press
from renewal.browser.test_patient_access import _overflowing, _redeem, _ring
from renewal.browser.test_patient_video import MEDIA_ARGS, TRACK_JS
from renewal.browser.test_prescribing import (
    ITEM,
    concurrent_sign,
    failed_callback,
    operations,
)
from renewal.browser.test_retention import (
    post_action,
    seed_manager,
    sign_in_manager,
    verify_package,
)
from renewal.browser.test_teleconsult import _room_operation
from renewal.browser.test_teleconsult import _worker as room_worker

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route

__all__ = ("availability_staff",)

TIMEOUT_MS: Final = 20_000
MIN_TARGET_PX: Final = 44
FORBIDDEN: Final = 403
CONFLICT: Final = 409
# Each run owns its civil day, so no run shares a slot with another.
RUNS: Final = (
    ("desktop", 1280, "2036-04-08", False),
    ("mobile", 375, "2036-04-09", False),
    ("recovery", 768, "2036-04-10", True),
)
SOAP: Final = ("subjective", "objective", "assessment", "plan")
NOTES: Final = {
    "subjective": "Relato sintético do dia de ensaio",
    "objective": "Exame sintético sem achados",
    "assessment": "Avaliação sintética do médico",
    "plan": "Plano sintético registrado",
}
CONSENT_TEXT: Final = (
    "Autorizo o atendimento por teleconsulta nesta clínica. A sessão não é "
    "gravada nem transcrita e posso revogar esta autorização antes de novos usos."
)
ANSWER: Final = "Motivo sintético da consulta"
# Visible English UI words that must never reach a pt-BR screen. Brand and
# protocol names (Clinic OS/Ops, PIX, SOAP, PDF) are deliberately absent.
ENGLISH: Final = re.compile(
    r"\b(Submit|Save|Cancel|Delete|Loading|Sign in|Sign out|Log in|Patient|"
    r"Appointment|Invoice|Settings|Search|Required|Password|Username|Back|"
    r"Next|Previous|Error|Continue|Download|Upload|Invitation|Physician|"
    r"Booking|Schedule|Access|Contacts|Retention|Availability|created|joined|"
    r"started|ended|patient|physician)\b"
)
# Every adapter the day touches, with what actually answers it in this run.
ADAPTERS: Final = {
    "runtime": {
        "label": "real",
        "detail": "Gunicorn, Django middleware/CSRF/TOTP/step-up, PostgreSQL "
        "RLS as clinic_app",
    },
    "outbox_worker": {
        "label": "real",
        "detail": "comms.execute_operation run as clinic_app in a subprocess",
    },
    "video_room": {
        "label": "mocked",
        "detail": "TELECONSULT_SYNTHETIC_PROVIDER room reference; no media "
        "leaves the device",
        "real_sandbox": "waiting_external",
    },
    "media_devices": {
        "label": "mocked",
        "detail": "Chromium --use-fake-device-for-media-stream",
    },
    "physician_registry": {
        "label": "mocked",
        "detail": "PHYSICIAN_SYNTHETIC_REGISTRY with an owner-provisioned "
        "SYNTHETIC- profile",
        "real_sandbox": "waiting_external",
    },
    "signature_provider": {
        "label": "mocked",
        "detail": "synthetic-signature-v1: this test posts the HMAC callback "
        "the provider would send",
        "real_sandbox": "waiting_external",
    },
    "pdf_rendering": {
        "label": "synthetic",
        "detail": "synthetic-pdf-v1 in-process renderer; bytes digest-pinned on review",
        "real_sandbox": "waiting_external",
    },
    "email_delivery": {
        "label": "mocked",
        "detail": "COMMS_SYNTHETIC_CHANNELS=email through the real outbox",
        "real_sandbox": "waiting_external",
    },
    "pix": {
        "label": "mocked",
        "detail": "synthetic-pix-v1 non-payable code; settlement attested "
        "through the staff form",
        "real_sandbox": "waiting_external",
    },
    "record_export": {
        "label": "real",
        "detail": "zip package with RFC 8785 manifest digest recomputed here",
    },
}
# Console lines the recovery run causes on purpose and asserts itself.
EXPECTED_RECOVERY_CONSOLE: Final = (
    "net::ERR_FAILED",
    "status of 403",
    "status of 404",
    "status of 409",
    "htmx:sendError",
    "htmx:afterRequest",
)
ADMIN_PERSONA: Final = "administração"
# Visible prose only: identifiers shown as <code> (signer subjects, reason
# codes, digests) are data, not copy.
PROSE_JS: Final = (
    "(() => { const body = document.body.cloneNode(true);"
    " body.querySelectorAll('code, script, style').forEach((e) => e.remove());"
    " document.documentElement.appendChild(body);"
    " const text = body.innerText; body.remove(); return text; })()"
)
CALLBACK_PATH: Final = "/prescription/signing/callback/synthetic-signature-v1/"
# The host of a date/time field stops matching :focus once focus moves to
# the browser's picker button inside its user-agent shadow tree.
PICKER_JS: Final = (
    "(() => { const e = document.activeElement;"
    " return e.matches('input:is([type=date], [type=time],"
    " [type=datetime-local])') && !e.matches(':focus'); })()"
)


@dataclass(slots=True)
class Day:
    """Everything one run of the clinic day shares."""

    name: str
    width: int
    day: str
    recovery: bool
    base: str
    staff: dict[str, str]
    root: Path
    tag: str = field(default_factory=lambda: secrets.token_hex(3))
    captures: list[str] = field(default_factory=list)
    keyboard: dict[str, list[str]] = field(default_factory=dict)
    facts: dict[str, object] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)

    @property
    def patient_name(self) -> str:
        return f"Paciente Sintética Jornada {self.tag}"

    @property
    def specialty_title(self) -> str:
        return f"Clínica geral jornada {self.tag}"

    @property
    def questionnaire_title(self) -> str:
        return f"Pré-consulta jornada {self.tag}"

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    @property
    def clinic(self) -> str:
        return self.staff["clinic_a"]

    @property
    def agenda(self) -> str:
        return self.url(f"/scheduling/clinics/{self.clinic}/agenda/day/{self.day}/1/")


# --------------------------------------------------------------------------
# Evidence helpers
# --------------------------------------------------------------------------


def capture(case: Day, page: Page, state: str) -> None:
    """Store a capture and prove pt-BR copy and reflow for the rendered screen."""
    destination = case.root / f"{case.name}-{state}-{_width(page)}.png"
    page.screenshot(path=str(destination), full_page=True)
    destination.chmod(0o600)
    lang = str(page.evaluate("document.documentElement.lang")).lower()
    assert lang.startswith("pt"), (state, lang)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        state,
        _overflowing(page),
    )
    english = sorted(set(ENGLISH.findall(str(page.evaluate(PROSE_JS)))))
    assert not english, (state, english)
    case.captures.append(destination.name)


def _width(page: Page) -> int:
    size = page.viewport_size
    assert size is not None
    return size["width"]


def keyboard_reaches(case: Day, page: Page, target: Locator, scene: str) -> None:
    """Tab from the main landmark to ``target``; every stop shows its ring."""
    page.locator("#main-content").focus()
    stops: list[str] = []
    for _ in range(80):
        page.keyboard.press("Tab")
        ring = _ring(page)
        label = str(
            page.evaluate(
                "(() => { const e = document.activeElement;"
                " return (e.getAttribute('value') || e.name || e.id"
                " || e.textContent.trim().slice(0, 30) || e.tagName); })()"
            )
        )
        if page.evaluate(PICKER_JS):
            # Chromium's own calendar/clock button inside a date or time
            # field holds focus here and draws the UA ring in its shadow.
            stops.append(f"{label}:picker")
            continue
        assert ring["style"] != "none", (scene, label, ring)
        assert ring["width"] != "0px", (scene, label, ring)
        stops.append(label)
        if target.evaluate("(element) => element === document.activeElement"):
            break
    else:
        pytest.fail(f"{scene}: keyboard never reached the control")
    box = target.bounding_box()
    assert box is not None
    assert box["height"] >= MIN_TARGET_PX, (scene, box)
    case.keyboard[scene] = stops
    capture(case, page, f"keyboard-{scene}")


def watch(case: Day, persona: str, page: Page) -> Page:
    """Record page errors and console errors for one persona's page."""
    errors = case.errors.setdefault(persona, [])
    page.set_default_timeout(TIMEOUT_MS)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    return page


def unexpected_errors(case: Day) -> dict[str, list[str]]:
    """Every recorded error that the recovery run did not cause on purpose."""
    allowed = EXPECTED_RECOVERY_CONSOLE if case.recovery else ("status of 409",)
    return {
        persona: [
            message
            for message in messages
            if not any(pattern in message for pattern in allowed)
        ]
        for persona, messages in case.errors.items()
    }


def commit_then_drop(page: Page, path: str, trigger: Callable[[], None]) -> None:
    """Let the server commit one POST, then lose its response in the browser.

    This is the interruption a user sees when the network drops after the
    request left: the server state changed, the screen never heard back.
    """
    dropped: list[int] = []

    def handler(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        response = route.fetch()
        dropped.append(response.status)
        route.abort("failed")

    page.route(re.compile(re.escape(path) + r"$"), handler)
    try:
        with page.expect_event("requestfailed"):
            trigger()
    finally:
        page.unroute(re.compile(re.escape(path) + r"$"))
    assert dropped, "the interrupted request never reached the server"


def owner_rows(
    case: Day, statement: str, params: list[object]
) -> list[tuple[object, ...]]:
    """Read stored rows for assertions only, through the fixture boundary."""
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [case.staff["organization"]],
        )
        return list(conn.execute(statement, params).fetchall())


def provision_physician_profile(staff: dict[str, str]) -> None:
    """Owner-provision the synthetic signing identity (no UI exists by design).

    ``apps/identity/physician-verification.md``: real provisioning and
    identity proofing are external prerequisites, never a staff screen.
    """
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_physicianprofile "
            "(id,organization_id,user_id,jurisdiction,registration_number,"
            "signing_subject,synthetic,status) "
            "VALUES (gen_random_uuid(),%s,%s,'SP',%s,%s,true,'unknown') "
            "ON CONFLICT DO NOTHING",
            [
                staff["organization"],
                staff["physician_a_id"],
                f"SYNTHETIC-CRM-{staff['physician_a_id'][:8].upper()}",
                f"synthetic:physician:{staff['physician_a_id']}",
            ],
        )


def runtime_rows(
    case: Day, statement: str, params: list[object]
) -> list[tuple[object, ...]]:
    """Read billing rows as the runtime role and reception actor, never owner.

    Billing policies call runtime-only guard functions, so the owner role
    cannot read them; the check runs exactly where the screens run.
    """
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [case.staff["organization"], case.staff["receptionist_id"]],
        )
        return list(conn.execute(statement, params).fetchall())


def document_pdf(staff: dict[str, str], document: str) -> bytes:
    """Decrypt the stored rendered PDF, the exact bytes a provider signs.

    ``pdf_bytes`` is a tenant envelope; the fixture boundary decrypts it with
    the run's synthetic KEK, the same material the runtime reads.
    """
    secret_dir = Path(os.environ["CLINIC_SECRET_DIR"])
    kek = (secret_dir / "tenant-kek.secret").read_text().strip()
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT clinic_app.protected_decrypt(%s, "
            "'prescription.prescriptiondocument.pdf_bytes', pdf_bytes) "
            "FROM clinic_app.prescription_prescriptiondocument WHERE id = %s",
            [kek, document],
        ).fetchone()
    assert row is not None
    return bytes(row[0])


def provider_signs(page: Page, case: Day, document: str) -> None:
    """Post the synthetic provider's authenticated signed report."""
    operation = _operation_row(case.staff, document)
    content = document_pdf(case.staff, document)
    assert hashlib.sha256(content).hexdigest() == operation["digest"]
    manifest: dict[str, str] = {
        "v": "clinic-synthetic-signature-v1",
        "operation_id": operation["operation_ref"],
        "content_digest": operation["digest"],
        "signer": operation["signer"],
        "signed_at": datetime.now(UTC).isoformat(),
    }
    manifest["signature"] = hmac.new(
        SYNTHETIC_SECRET, rfc8785.dumps(manifest), hashlib.sha256
    ).hexdigest()
    body = json.dumps(
        {
            "event_id": f"event-{secrets.token_hex(8)}",
            "operation_id": operation["operation_ref"],
            "status": "signed",
            "signed_bytes": base64.b64encode(
                content + MARKER + rfc8785.dumps(manifest) + END
            ).decode("ascii"),
        }
    ).encode()
    response = page.request.post(
        f"{case.base}{CALLBACK_PATH}",
        data=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: hmac.new(
                SYNTHETIC_SECRET, body, hashlib.sha256
            ).hexdigest(),
        },
    )
    assert response.status == 200


def answer_challenge(page: Page, staff: dict[str, str]) -> None:
    """Complete a real TOTP challenge for the physician's current device."""
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [staff["physician_a_id"]],
        )
        connection.execute(
            "UPDATE clinic_app.otp_totp_totpdevice SET last_t = -1, drift = 0 "
            "WHERE user_id = %s",
            [staff["physician_a_id"]],
        )
    token = TOTP(bytes.fromhex(staff["totp_key"]), 30, 0, 6, 1).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    with page.expect_navigation():
        page.locator("button[type=submit]").click()


def press_stepped(page: Page, staff: dict[str, str], action: str) -> None:
    """Press an action that may demand recent verification, then press again."""
    press(page, action)
    if "/auth/step-up/" in page.url or "/auth/verify/" in page.url:
        answer_challenge(page, staff)
        press(page, action)


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------


def configure_clinic(case: Day, admin: Page, manager: dict[str, str]) -> None:
    """The clinic administrator publishes the day's templates and texts."""
    sign_in_manager(admin, case.base, case.staff, manager)
    with admin.expect_navigation():
        admin.locator("a[data-module=settings]").click()
    specialty = admin.locator('[data-settings-form="specialty"]')
    specialty.locator("#id_key").fill(f"jornada-{case.tag}")
    specialty.locator("#id_title").fill(case.specialty_title)
    for name in SOAP:
        specialty.locator(f"#id_{name}").fill(f"Orientação sintética: {name}")
    press(admin, "specialty")
    expect(
        admin.locator('[data-specialty-version="1"]', has_text=case.specialty_title)
    ).to_be_visible()
    admin.locator("#id_purpose").select_option("teleconsultation")
    admin.locator("#id_text").fill(CONSENT_TEXT)
    press(admin, "consent")
    questionnaire = admin.locator('[data-settings-form="questionnaire"]')
    questionnaire.locator("#questionnaire_key").fill(f"pre-{case.tag}")
    questionnaire.locator("#questionnaire_title").fill(case.questionnaire_title)
    questionnaire.locator("#questionnaire_q1_label").fill("Qual o motivo da consulta?")
    questionnaire.locator("#questionnaire_q1_required").check()
    questionnaire.locator("#questionnaire_q1_max_length").fill("200")
    questionnaire.locator("#questionnaire_q2_label").fill("Prefere retorno por")
    questionnaire.locator("#questionnaire_q2_type").select_option("selection")
    questionnaire.locator("#questionnaire_q2_required").check()
    questionnaire.locator("#questionnaire_q2_options").fill("Telefone\nMensagem")
    questionnaire.locator("#questionnaire_q3_label").fill("Tem alergia conhecida?")
    questionnaire.locator("#questionnaire_q3_type").select_option("boolean")
    press(admin, "questionnaire")
    expect(
        admin.locator(
            '[data-questionnaire-version="1"]', has_text=case.questionnaire_title
        )
    ).to_be_visible()
    capture(case, admin, "admin-configured")


def open_patient_row(case: Day, page: Page, button: str) -> None:
    """Search the registry and press one named action on the patient's row."""
    page.goto(case.url(f"/intake/clinics/{case.clinic}/patients/"))
    page.locator("#id_q").fill(case.patient_name)
    with page.expect_response(lambda response: response.request.method == "POST"):
        page.locator("#patient-search-form button[type=submit]").click()
    wait_for_js(page, SETTLED_JS)
    row = page.locator(".intake-table tbody tr", has_text=case.patient_name)
    with page.expect_navigation():
        row.get_by_role("button", name=re.compile(f"^{button}")).click()


def reception_prepares(case: Day, reception: Page) -> str:
    """Availability, registration, booking, verified contact and invitation."""
    staff = case.staff
    _sign_in_receptionist(reception, case.base, staff)
    listing = f"/scheduling/clinics/{case.clinic}/availability/"
    reception.goto(case.url(listing))
    fill_availability(reception, staff["physician_a"], case.day, "08:00", "12:00")
    _submit_expecting_success(reception, listing)
    capture(case, reception, "reception-availability")

    # Registration from the empty search, the way the agenda points to it.
    reception.goto(case.url(f"/intake/clinics/{case.clinic}/patients/"))
    reception.locator("#id_q").fill(case.patient_name)
    with reception.expect_response(lambda r: r.request.method == "POST"):
        reception.locator("#patient-search-form button[type=submit]").click()
    wait_for_js(reception, SETTLED_JS)
    with reception.expect_navigation():
        reception.locator(".intake-empty a.button--secondary").click()
    reception.locator("#id_full_name").fill(case.patient_name)
    reception.locator("#id_birth_date").fill("1988-02-29")
    with reception.expect_navigation():
        reception.locator("#patient-create-panel button[type=submit]").click()
    expect(reception.locator("#intake-registered")).to_contain_text(
        "Paciente cadastrado"
    )
    capture(case, reception, "reception-registered")
    case.facts["patient_id"] = owner_rows(
        case,
        "SELECT id::text FROM clinic_app.intake_patient p WHERE EXISTS ("
        "SELECT 1 FROM clinic_app.intake_patientclinicenrollment e "
        "WHERE e.patient_id = p.id AND e.clinic_id = %s) "
        "ORDER BY p.created_at DESC LIMIT 1",
        [case.clinic],
    )[0][0]

    book(case, reception)
    verified_contact(case, reception)

    open_patient_row(case, reception, "Acesso")
    expect(reception.locator("h1")).to_have_text("Acesso do paciente")
    with reception.expect_navigation():
        reception.get_by_role("button", name="Emitir novo convite").click()
    code = reception.locator("#issued-code").inner_text()
    assert code
    capture(case, reception, "reception-invitation")
    return code


def book(case: Day, reception: Page) -> None:
    """Book 09:00-09:30 through the screen; the recovery run drops the reply."""
    open_patient_row(case, reception, "Agendar consulta")
    expect(reception.locator("#booking-patient")).to_have_text(case.patient_name)
    reception.locator("#id_practitioner").select_option(label=case.staff["physician_a"])
    reception.locator("#id_start_local").fill(f"{case.day}T09:00")
    reception.locator("#id_end_local").fill(f"{case.day}T09:30")
    submit = reception.locator("#scheduling-booking form button[type=submit]")
    keyboard_reaches(case, reception, submit, "booking")
    if case.recovery:
        # The server books, the browser never hears back; retry the same form.
        commit_then_drop(
            reception,
            f"/scheduling/clinics/{case.clinic}/appointments/new/",
            submit.click,
        )
        wait_for_js(reception, SETTLED_JS)
        # The dropped reply is announced: an alert names the failure and the
        # recovery, and the submit is usable again for the deliberate retry.
        notice = reception.locator("[data-network-error]")
        expect(notice).to_be_visible()
        expect(notice).to_contain_text("A solicitação não foi concluída")
        expect(notice).to_be_focused()
        expect(submit).to_be_enabled()
        capture(case, reception, "booking-interrupted")
    with reception.expect_navigation(url=re.compile(r"/agenda/day/")):
        submit.click()
    expect(reception.locator("#appointment-booked")).to_contain_text(
        "Consulta agendada"
    )
    booked = owner_rows(
        case,
        "SELECT id::text FROM clinic_app.scheduling_appointment "
        "WHERE patient_id = %s AND status = 'scheduled'",
        [case.facts["patient_id"]],
    )
    assert len(booked) == 1, booked
    case.facts["appointment_id"] = booked[0][0]
    capture(case, reception, "reception-booked")


def verified_contact(case: Day, reception: Page) -> None:
    """Record and verify the e-mail the document delivery will use."""
    open_patient_row(case, reception, "Contatos")
    item = reception.locator(
        ".contacts-item", has=reception.locator("h3", has_text="E-mail")
    )
    with reception.expect_navigation():
        item.locator("form").first.locator("button").click()
    reception.locator("#id_destination").fill(f"jornada-{case.tag}@example.invalid")
    with reception.expect_navigation():
        reception.get_by_role("button", name="Salvar destino").click()
    item = reception.locator(
        ".contacts-item", has=reception.locator("h3", has_text="E-mail")
    )
    with reception.expect_navigation():
        item.get_by_role("button", name=re.compile("^Marcar como verificado")).click()
    capture(case, reception, "reception-contact-verified")


def agenda_row(case: Day, physician: Page) -> Locator:
    physician.goto(case.agenda)
    return physician.locator(".agenda-row", has_text=case.patient_name)


def assign_questionnaire(case: Day, physician: Page) -> None:
    """The physician assigns the published questionnaire from the agenda row."""
    _sign_in_physician(physician, case.base, case.staff)
    row = agenda_row(case, physician)
    capture(case, physician, "physician-agenda")
    with physician.expect_navigation():
        row.locator('button[value="appointment"]').click()
    expect(physician.locator("[data-assign]")).to_be_visible()
    physician.locator("#template-id").select_option(
        label=f"{case.questionnaire_title} · versão 1"
    )
    assign = physician.locator('button[value="assign"]')
    keyboard_reaches(case, physician, assign, "assign")
    press(physician, "assign")
    expect(physician.locator('[role="status"]')).to_contain_text(
        "Questionário atribuído"
    )
    if case.recovery:
        # A repeated assignment converges on the first; nothing is duplicated.
        physician.locator("#template-id").select_option(
            label=f"{case.questionnaire_title} · versão 1"
        )
        press(physician, "assign")
        expect(physician.locator('[role="status"]')).to_contain_text(
            "já estava atribuído"
        )
    expect(physician.locator('section[data-state="draft"]')).to_have_count(1)
    capture(case, physician, "physician-assigned")


def patient_arrives(case: Day, patient: Page, code: str) -> None:
    """Redeem the invitation, answer the questionnaire and accept consent."""
    _redeem(patient, case.base, case.clinic, code)
    patient.wait_for_url("**/patient/")
    for link in (
        "Questionários antes da consulta",
        "Seus consentimentos",
        "Teleconsulta",
        "Suas cobranças",
        "Seus registros",
        "Seus documentos",
    ):
        expect(patient.get_by_role("link", name=link)).to_be_visible()
    capture(case, patient, "patient-home")
    with patient.expect_navigation():
        patient.get_by_role("link", name="Questionários antes da consulta").click()
    press(patient, "open")
    patient.locator("#id_q_1").fill(ANSWER)
    patient.locator("#id_q_2").select_option("Mensagem")
    patient.locator("#id_q_3").select_option("False")
    submit = patient.locator('button[value="submit"]')
    keyboard_reaches(case, patient, submit, "questionnaire")
    with patient.expect_navigation():
        patient.keyboard.press("Enter")
    expect(patient.locator("[data-template-version]")).to_have_attribute(
        "data-state", "submitted"
    )
    capture(case, patient, "patient-questionnaire-submitted")

    patient.goto(case.url("/patient/consent/"))
    form = patient.locator("form", has_text="Teleconsulta").first
    with patient.expect_navigation():
        form.locator('button[value="read"]').click()
    patient.locator("#id_accepted").focus()
    patient.keyboard.press("Space")
    expect(patient.locator("#id_accepted")).to_be_checked()
    accept = patient.locator('button[value="accept"]')
    keyboard_reaches(case, patient, accept, "consent")
    with patient.expect_navigation():
        patient.keyboard.press("Enter")
    expect(patient.locator("[data-receipt]").first).to_have_attribute(
        "data-state", "accepted"
    )
    capture(case, patient, "patient-consent-accepted")


def physician_reads_answers(case: Day, physician: Page) -> None:
    row = agenda_row(case, physician)
    with physician.expect_navigation():
        row.locator('button[value="appointment"]').click()
    submitted = physician.locator('section[data-state="submitted"]')
    expect(submitted).to_have_count(1)
    with physician.expect_navigation():
        submitted.locator('button[value="inspect"]').click()
    expect(physician.locator("main")).to_contain_text(ANSWER)
    expect(physician.locator("main")).to_contain_text("Mensagem")
    capture(case, physician, "physician-answers")


def write_notes(case: Day, physician: Page) -> str:
    """Open the encounter from the agenda, pick the template and save notes."""
    row = agenda_row(case, physician)
    with physician.expect_navigation():
        row.locator('button[value="open"]').click()
    physician.locator("#template-id").select_option(
        label=f"{case.specialty_title} · versão 1"
    )
    press(physician, "template")
    for name, value in NOTES.items():
        physician.locator(f"#id_{name}").fill(value)
    encounter_url = case.url(f"/ehr/clinics/{case.clinic}/encounter/")
    save = physician.locator('button[value="save"]')
    keyboard_reaches(case, physician, save, "notes")
    if case.recovery:
        commit_then_drop(
            physician, f"/ehr/clinics/{case.clinic}/encounter/", save.click
        )
        physician.goto(encounter_url)
        # Nothing typed was lost: the committed save is what reloads.
        for name, value in NOTES.items():
            expect(physician.locator(f"#id_{name}")).to_have_value(value)
        capture(case, physician, "notes-recovered")
    else:
        press(physician, "save")
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "saved")
    version = physician.locator("[data-version]").get_attribute("data-version")
    encounter = physician.locator("[data-encounter]").get_attribute("data-encounter")
    assert version
    assert encounter
    expect(physician.locator("[data-version]")).to_have_attribute("data-revision", "2")
    case.facts["encounter_id"] = encounter
    case.facts["version_id"] = version
    capture(case, physician, "physician-notes-saved")
    return encounter


def video_visit(case: Day, physician: Page, patient: Page, encounter: str) -> None:
    """Create, provision, join, start and end the teleconsult session."""
    staff_url = case.url(f"/teleconsult/clinics/{case.clinic}/")
    patient_url = case.url("/patient/teleconsult/")
    physician.goto(case.url(f"/ehr/clinics/{case.clinic}/encounter/"))
    with physician.expect_navigation():
        physician.locator("[data-teleconsult-link]").click()
    assert physician.url == staff_url
    expect(physician.locator("[data-synthetic-room]")).to_be_visible()
    with physician.expect_navigation():
        physician.locator(
            f'form:has(input[name="encounter_id"][value="{encounter}"]) '
            'button[value="create"]'
        ).click()
    session = owner_rows(
        case,
        "SELECT id::text FROM clinic_app.teleconsult_teleconsultsession "
        "WHERE encounter_id = %s",
        [encounter],
    )
    assert len(session) == 1, session
    session_id = str(session[0][0])
    case.facts["session_id"] = session_id
    room_worker(_room_operation(case.staff, session_id), "sent", case.root)
    physician.goto(staff_url)
    card = physician.locator(f'[data-session="{session_id}"]')
    expect(card).to_have_attribute("data-state", "waiting")
    capture(case, physician, "physician-session-waiting")

    panel = patient_enters_room(case, patient, session_id)
    if case.recovery:
        connection_recovers(case, patient, panel)

    for action in ("join", "start"):
        physician.goto(staff_url)
        with physician.expect_navigation():
            card.locator(f'button[value="{action}"]').click()
    physician.goto(staff_url)
    expect(card).to_have_attribute("data-state", "active")
    patient.locator("[data-refresh-status]").click()
    expect(panel).to_have_attribute("data-state", "active")
    capture(case, patient, "patient-room-active")
    with physician.expect_navigation():
        card.locator('button[value="end"]').click()
    expect(card).to_have_attribute("data-state", "ended")
    capture(case, physician, "physician-session-ended")
    patient.locator("[data-refresh-status]").click()
    expect(panel).to_have_attribute("data-state", "ended")
    capture(case, patient, "patient-room-ended")
    # A terminal session never reopens; the retry is a conflict, not a room.
    assert (
        post_action(patient, patient_url, {"action": "join", "session_id": session_id})
        == CONFLICT
    )
    sessions = owner_rows(
        case,
        "SELECT count(*) FROM clinic_app.teleconsult_teleconsultsession "
        "WHERE encounter_id = %s",
        [encounter],
    )
    assert sessions == [(1,)]


def patient_enters_room(case: Day, patient: Page, session_id: str) -> Locator:
    """Waiting room, explicit device test, then the synthetic room."""
    patient.goto(case.url("/patient/teleconsult/"))
    expect(patient.locator("[data-synthetic-room]")).to_be_visible()
    expect(patient.locator(f'[data-session="{session_id}"]')).to_have_attribute(
        "data-state", "waiting"
    )
    patient.locator("[data-device-test]").click()
    expect(patient.locator("[data-device-check]")).to_have_attribute(
        "data-device-state", "ready"
    )
    capture(case, patient, "patient-waiting-devices")
    patient.locator("[data-device-stop]").click()
    with patient.expect_navigation():
        patient.locator('button[value="join"]').click()
    panel = patient.locator("#room-panel")
    expect(panel).to_have_attribute("data-connection", "connected")
    expect(patient.locator("#room-name")).to_contain_text(f"tc-{session_id}")
    expect(patient.locator("[data-synthetic-room]")).to_be_visible()
    capture(case, patient, "patient-room")
    return panel


def connection_recovers(case: Day, patient: Page, panel: Locator) -> None:
    """A dropped connection is announced and recovers in the same room."""
    patient.context.set_offline(offline=True)
    expect(panel).to_have_attribute("data-connection", "offline")
    expect(patient.locator("[data-reconnect]")).to_be_visible()
    capture(case, patient, "video-offline")
    # A manual retry while still offline stays honest about the outage;
    # coming back online reconnects the same room without a new session.
    patient.locator("[data-reconnect]").click()
    expect(panel).to_have_attribute("data-connection", "offline")
    patient.context.set_offline(offline=False)
    expect(panel).to_have_attribute("data-connection", "connected")
    assert patient.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}
    capture(case, patient, "video-reconnected")


def finalize_notes(case: Day, physician: Page) -> None:
    physician.goto(case.url(f"/ehr/clinics/{case.clinic}/encounter/"))
    press_stepped(physician, case.staff, "finalize")
    expect(physician.locator("[data-version]")).to_have_attribute(
        "data-state", "finalized"
    )
    expect(physician.locator("[data-finalization]")).to_have_attribute(
        "data-finalization", "local"
    )
    capture(case, physician, "physician-finalized")


def sign_document(case: Day, physician: Page, anonymous: Page) -> str:
    """Author, review, sign (synthetic provider), verify publicly, release."""
    base = case.base
    physician.goto(case.url(f"/ehr/clinics/{case.clinic}/encounter/"))
    with physician.expect_navigation():
        physician.get_by_role("button", name="Prescrição sintética").click()
    press(physician, "create")
    for name, value in ITEM.items():
        physician.locator(f"#id_items-0-{name}").fill(value)
    press(physician, "save")
    press(physician, "render_document")
    expect(physician.locator("[data-step]")).to_have_attribute("data-step", "review")
    document = physician.locator("[data-document]").first.get_attribute("data-document")
    assert document
    case.facts["document_id"] = document
    review_url = physician.url
    expect(physician.locator("main")).to_contain_text("ensaio sintético")
    capture(case, physician, "physician-review")
    sign = physician.locator('#sign-form button[value="sign_document"]')
    keyboard_reaches(case, physician, sign, "sign")
    if case.recovery:
        signature_recovers(case, physician, review_url, document)
    else:
        press_stepped(physician, case.staff, "sign_document")
    signing_url = physician.url
    expect(physician.locator("[data-state]")).to_have_attribute("data-state", "signing")
    provider_signs(physician, case, document)
    physician.goto(signing_url)
    expect(physician.locator("[data-state]")).to_have_attribute(
        "data-state", "rehearsal_complete"
    )
    expect(physician.locator("[data-result]")).to_contain_text("sem validade")
    capture(case, physician, "physician-signed-rehearsal")

    handle = str(_document_row(case.staff, document)["handle"])
    verified = anonymous.goto(f"{base}/prescription/verify/{handle}/")
    assert verified is not None
    assert verified.status == 200
    expect(anonymous.locator("#verify-status")).to_have_attribute(
        "data-status", "rehearsal_complete"
    )
    assert case.patient_name not in anonymous.content()
    capture(case, anonymous, "public-verify")

    release_and_deliver(case, physician, document)
    return handle


def signature_recovers(
    case: Day, physician: Page, review_url: str, document: str
) -> None:
    """Parallel submits make one attempt; a provider failure is restartable."""
    results = concurrent_sign(physician, review_url)
    assert len({location for _, location in results}) == 1, results
    if "/auth/" in results[0][1]:
        physician.goto(f"{case.base}{results[0][1]}")
        answer_challenge(physician, case.staff)
        results = concurrent_sign(physician, review_url)
    assert [status for status, _ in results] == [302, 302], results
    assert operations(case.staff, document) == [("signing", "")]
    headers, body = failed_callback(case.staff, document)
    response = physician.request.post(
        f"{case.base}{CALLBACK_PATH}",
        data=body,
        headers={"Content-Type": "application/json", **headers},
    )
    assert response.status == 200
    physician.goto(f"{case.base}{results[0][1]}")
    expect(physician.locator("[data-state]")).to_have_attribute("data-state", "failed")
    capture(case, physician, "signature-provider-failed")
    press_stepped(physician, case.staff, "restart_signature")
    assert operations(case.staff, document) == [
        ("failed", "provider_reported"),
        ("signing", ""),
    ]


def release_and_deliver(case: Day, physician: Page, document: str) -> None:
    """Release to the patient and deliver the link through the real outbox."""
    physician.goto(case.url(f"/prescription/clinics/{case.clinic}/draft/"))
    row = physician.locator(f'[data-document="{document}"]')
    with physician.expect_navigation():
        row.locator('button[value="release_document"]').click()
    with physician.expect_navigation():
        physician.locator(
            f'[data-document="{document}"] button[value="deliver_document"]'
        ).click()
    delivery = owner_rows(
        case,
        "SELECT id::text FROM clinic_app.comms_integrationoperation "
        "WHERE subject_id = %s AND subject_type = 'prescription.document'",
        [document],
    )
    assert len(delivery) == 1, delivery
    receipt = delivery_worker(str(delivery[0][0]), case.root)
    assert receipt["outcomes"] == ["succeeded"]
    assert receipt["runtime_role"] == "clinic_app"
    capture(case, physician, "physician-document-delivered")


def patient_downloads_document(case: Day, patient: Page, document: str) -> None:
    patient.goto(case.url("/patient/"))
    with patient.expect_navigation():
        patient.locator("#documents-link").click()
    row = patient.locator(f'[data-document="{document}"]')
    expect(row).to_be_visible()
    expect(row).to_contain_text("ensaio sintético")
    capture(case, patient, "patient-documents")
    with patient.expect_download() as download:
        row.locator('button[value="download"]').click()
    assert download.value.suggested_filename.endswith(".pdf")


def charge_and_receipt(case: Day, reception: Page, patient: Page) -> None:
    """Reception charges the visit; the patient pays synthetically; receipt."""
    with reception.expect_navigation():
        reception.locator("a[data-module=billing]").click()
    ledger = case.url(f"/billing/clinics/{case.clinic}/charges/")
    assert reception.url == ledger

    def create() -> None:
        reception.locator("#id_patient_id").select_option(label=case.patient_name)
        reception.locator("#id_amount").fill(AMOUNT)

    create()
    create_button = reception.locator('button[value="create"]')
    keyboard_reaches(case, reception, create_button, "charge")
    if case.recovery:
        commit_then_drop(
            reception, f"/billing/clinics/{case.clinic}/charges/", create_button.click
        )
        reception.goto(ledger)
        create()
    press(reception, "create")
    expect_state(reception, "draft")
    invoice = invoice_id_of(reception.url)
    charges = runtime_rows(
        case,
        "SELECT id::text FROM clinic_app.billing_invoice WHERE patient_id = %s",
        [case.facts["patient_id"]],
    )
    assert charges == [(invoice,)], charges
    case.facts["invoice_id"] = invoice
    press(reception, "issue")
    expect_state(reception, "issued")
    press(reception, "code")
    expect_state(reception, "pending")
    code = str(reception.locator("[data-code]").text_content())
    assert code.startswith(CODE_PREFIX)
    assert qr_renders(reception)
    expect(reception.locator("main")).to_contain_text("não pagável")
    press(reception, "release")
    capture(case, reception, "reception-charge-pending")

    patient.goto(case.url("/patient/"))
    with patient.expect_navigation():
        patient.locator("#charges-link").click()
    expect(patient.locator("[data-amount]")).to_have_text(AMOUNT_TEXT)
    with patient.expect_navigation():
        patient.locator("[data-charge] a").click()
    expect_state(patient, "pending")
    assert str(patient.locator("[data-code]").text_content()) == code
    expect(patient.locator("main")).to_contain_text("não pagável")
    capture(case, patient, "patient-pix-instructions")

    settle(case, reception, invoice)
    receipt = reception.locator("[data-receipt]").get_attribute("data-receipt")
    assert receipt
    patient.goto(case.url(f"/patient/charges/{invoice}/"))
    expect_state(patient, "paid")
    expect(patient.locator("[data-receipt]")).to_have_attribute("data-receipt", receipt)
    expect_synthetic_receipt(patient)
    capture(case, patient, "patient-receipt")


def expect_synthetic_receipt(page: Page) -> None:
    """A rehearsal receipt never reads as real money, on visit or refresh."""
    disclosure = page.locator("[data-receipt] [data-synthetic-receipt]")
    expect(disclosure).to_be_visible()
    page.reload()
    expect(disclosure).to_be_visible()


def settle(case: Day, reception: Page, invoice: str) -> None:
    """Attest one settlement; the recovery run drops the reply and replays."""
    invoice_url = case.url(f"/billing/clinics/{case.clinic}/charges/{invoice}/")
    reception.goto(invoice_url)
    reference = str(uuid4())
    if case.recovery:
        reception.locator("#id_confirmation_reference").fill(reference)
        reception.locator("#settlement-form #id_amount").fill(AMOUNT)
        reception.locator("#id_attested").check()
        commit_then_drop(
            reception,
            f"/billing/clinics/{case.clinic}/charges/{invoice}/",
            reception.locator('button[value="confirm"]').click,
        )
        # The settlement committed; reloading shows it instead of a retry form.
        reception.goto(invoice_url)
        expect_state(reception, "paid")
        # Replaying the same attested reference converges on the one receipt.
        status = post_action(
            reception,
            invoice_url,
            {
                "action": "confirm",
                "confirmation_reference": reference,
                "amount": AMOUNT,
                "attested": "on",
            },
        )
        assert status == 302, status
        reception.goto(invoice_url)
    else:
        confirm_settlement(reception, AMOUNT)
    expect_state(reception, "paid")
    settled = runtime_rows(
        case,
        "SELECT (SELECT count(*) FROM clinic_app.billing_settlement "
        "WHERE invoice_id = %s), (SELECT count(*) FROM clinic_app.billing_receipt "
        "r JOIN clinic_app.billing_settlement s ON s.id = r.settlement_id "
        "WHERE s.invoice_id = %s)",
        [invoice, invoice],
    )
    assert settled == [(1, 1)], settled
    expect_synthetic_receipt(reception)
    capture(case, reception, "reception-paid")


def export_records(case: Day, physician: Page, patient: Page) -> None:
    """Release the finalized version and verify both export packages."""
    version = str(case.facts["version_id"])
    patient_id = str(case.facts["patient_id"])
    physician.goto(case.url(f"/retention/clinics/{case.clinic}/"))
    with physician.expect_navigation():
        physician.locator(
            f'#releasable-list li[data-version="{version}"] button[value="release"]'
        ).click()
    with physician.expect_download() as received:
        physician.locator(
            f'#export-patient-list li[data-patient="{patient_id}"] '
            'button[value="export"]'
        ).click()
    package = received.value.path()
    assert package is not None
    staff_manifest = verify_package(package.read_bytes(), version)
    assert staff_manifest["kind"] == "staff"
    capture(case, physician, "physician-export")

    patient.goto(case.url("/patient/"))
    with patient.expect_navigation():
        patient.locator("#records-link").click()
    expect(patient.locator("main")).to_contain_text(NOTES["subjective"])
    export = patient.locator("#export-button")
    keyboard_reaches(case, patient, export, "export")
    with patient.expect_download() as received:
        export.click()
    package = received.value.path()
    assert package is not None
    patient_manifest = verify_package(package.read_bytes(), version)
    assert patient_manifest["kind"] == "patient"
    case.facts["exports"] = {
        "staff": staff_manifest["manifest_sha256"],
        "patient": patient_manifest["manifest_sha256"],
    }
    capture(case, patient, "patient-export")


def denials(case: Day, reception: Page, physician: Page, patient: Page) -> None:
    """Revoke the patient's access and try staff sessions on the wrong clinic."""
    open_patient_row(case, reception, "Acesso")
    with reception.expect_navigation():
        reception.get_by_role("button", name="Revogar acesso").first.click()
    capture(case, reception, "reception-access-revoked")
    for path in ("/patient/", "/patient/documents/", "/patient/charges/"):
        response = patient.goto(case.url(path))
        assert response is not None
        assert response.status == FORBIDDEN, path
    capture(case, patient, "patient-revoked")
    other = case.staff["clinic_b"]
    for page, path in (
        (reception, f"/billing/clinics/{other}/charges/"),
        (reception, f"/intake/clinics/{other}/patients/"),
        (physician, f"/retention/clinics/{other}/"),
        (physician, f"/teleconsult/clinics/{other}/"),
    ):
        response = page.goto(case.url(path))
        assert response is not None
        assert response.status in (FORBIDDEN, 404), (path, response.status)
        body = page.content()
        assert case.patient_name not in body
    capture(case, physician, "physician-wrong-clinic")


def reflow_scenes(case: Day, patient: Page) -> None:
    """320 px reflow, forced colors and reduced motion on the patient portal."""
    patient.set_viewport_size({"width": 320, "height": 900})
    patient.goto(case.url("/patient/"))
    capture(case, patient, "patient-home-reflow")
    patient.emulate_media(forced_colors="active", reduced_motion="reduce")
    patient.goto(case.url(f"/patient/charges/{case.facts['invoice_id']}/"))
    capture(case, patient, "patient-receipt-forced-colors")
    patient.emulate_media(forced_colors="none", reduced_motion="no-preference")


def _context(browser: Browser, case: Day, *, media: bool) -> BrowserContext:
    context = browser.new_context(
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": case.width, "height": 900},
    )
    if media:
        context.grant_permissions(["camera", "microphone"], origin=case.base)
    return context


@pytest.mark.parametrize(
    ("name", "width", "day", "recovery"), RUNS, ids=[run[0] for run in RUNS]
)
def test_synthetic_clinic_day(  # noqa: PLR0913 - the day needs its full context
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    name: str,
    width: int,
    day: str,
    recovery: bool,
) -> None:
    root = renewal_artifact_root / "end-to-end"
    root.mkdir(mode=0o700, exist_ok=True)
    case = Day(
        name=name,
        width=width,
        day=day,
        recovery=recovery,
        base=renewal_base_url,
        staff=availability_staff,
        root=root,
    )
    provision_physician_profile(case.staff)
    manager = seed_manager(case.staff)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=os.environ["CLINIC_RENEWAL_BROWSER_EXECUTABLE"],
            args=list(MEDIA_ARGS),
        )
        contexts: list[BrowserContext] = []
        try:
            pages: dict[str, Page] = {}
            for persona, media in (
                (ADMIN_PERSONA, False),
                ("recepção", False),
                ("médico", True),
                ("paciente", True),
                ("verificação pública", False),
            ):
                context = _context(browser, case, media=media)
                contexts.append(context)
                pages[persona] = watch(case, persona, context.new_page())
            admin = pages[ADMIN_PERSONA]
            reception = pages["recepção"]
            physician = pages["médico"]
            patient = pages["paciente"]
            anonymous = pages["verificação pública"]

            configure_clinic(case, admin, manager)
            code = reception_prepares(case, reception)
            assign_questionnaire(case, physician)
            patient_arrives(case, patient, code)
            physician_reads_answers(case, physician)
            encounter = write_notes(case, physician)
            video_visit(case, physician, patient, encounter)
            finalize_notes(case, physician)
            handle = sign_document(case, physician, anonymous)
            patient_downloads_document(case, patient, str(case.facts["document_id"]))
            charge_and_receipt(case, reception, patient)
            export_records(case, physician, patient)
            if width == min(run[1] for run in RUNS):
                reflow_scenes(case, patient)
            if recovery:
                denials(case, reception, physician, patient)

            unexpected = unexpected_errors(case)
            assert not any(unexpected.values()), unexpected
            (root / f"report-{name}-{width}.json").write_text(
                json.dumps(
                    {
                        "run": name,
                        "width": width,
                        "day": day,
                        "recovery": recovery,
                        "runtime_role": "clinic_app",
                        "adapters": ADAPTERS,
                        "real_sandbox_adapters": [],
                        "facts": case.facts,
                        "public_verify_handle": handle,
                        "captures": case.captures,
                        "keyboard_paths": case.keyboard,
                        "console_errors": case.errors,
                        "unexpected_console_errors": unexpected,
                        "locale": "pt-BR",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        finally:
            for context in contexts:
                context.close()
            browser.close()
