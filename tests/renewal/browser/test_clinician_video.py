"""Clinician workspace: video beside encounter notes, on the real clinic_app stack.

The physician joins a provisioned synthetic room with real fake-device media,
creates and edits the SOAP draft next to the video, switches panels on small
screens without losing edits, saves explicitly, finalizes through the real
step-up challenge and amends. Failures are real: a database constraint
rejects a save, the browser goes offline, the stored room operation fails,
the physician switches to another patient's room with unsaved text, ends the
video with unsaved text and leaves the room. Every write is checked against
the stored rows; a session identifier never reaches another patient's note.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django_otp.oath import TOTP
from playwright.sync_api import expect, sync_playwright
from psycopg.types.json import Jsonb

from renewal.browser._protected import rename_patient
from renewal.browser.test_amendments import (
    stored_encounter,
    stored_versions,
    swap_totp_device,
)
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_encounter import FIELDS, stored
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_patient_video import MEDIA_ARGS, TRACK_JS
from renewal.browser.test_retention import post_action, seed_manager, sign_in_manager
from renewal.browser.test_teleconsult import (
    _accept_consent,
    _create_session,
    _open_encounter,
    _publish_consent,
    _room_operation,
    _seed,
    _worker,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Dialog, Page, Route

__all__ = ("availability_staff",)
DAYS = {1280: "2035-08-06", 768: "2035-08-07", 375: "2035-08-08"}
NAMES = ("Paciente Sintético Um", "Paciente Sintético Dois")
WIDE_PX = 1024
FORBIDDEN = 403
UNAVAILABLE = 503
OK = 200
FIRST = {
    "subjective": "Relato sintético na teleconsulta",
    "objective": "Exame sintético por vídeo",
    "assessment": "Avaliação sintética",
    "plan": "Plano sintético",
}
PRESERVATION_DAY = "2035-08-09"
SENT = "Relato enviado no salvamento"
LATE = "Relato enviado no salvamento, complementado enquanto a resposta chegava"
REJECTED = "Falha sintética"
REJECTED_LATE = "Falha sintética corrigida enquanto a resposta chegava"
PENDING_PLAN = "Plano ainda não salvo"
NATIVE = {
    "subjective": "Relato nativo digitado antes de iniciar",
    "objective": "Achado nativo digitado antes de encerrar",
}
FOCUS_RING_JS = """() => {
  const el = document.activeElement;
  const style = getComputedStyle(el);
  return {id: el.id, outline: style.outlineStyle,
    width: parseFloat(style.outlineWidth), height: el.getBoundingClientRect().height};
}"""


@dataclass(frozen=True, slots=True)
class _Case:
    """Everything one viewport's journey shares."""

    base: str
    staff: dict[str, str]
    day: str
    root: Path
    width: int
    staff_url: str
    patient_url: str

    @property
    def wide(self) -> bool:
        return self.width >= WIDE_PX


def _capture(page: Page, case: _Case, state: str) -> None:
    page.screenshot(path=str(case.root / f"{state}-{case.width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _seed_patient(case: _Case, hour: int, name: str) -> dict[str, str]:
    data = _seed(case.staff, case.day, hour)
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        rename_patient(
            conn,
            organization=case.staff["organization"],
            clinic=case.staff["clinic_a"],
            patient=data["patient"],
            enrollment=data["enrollment"],
            actor=case.staff["receptionist_id"],
            name=name,
        )
    data["name"] = name
    return data


def _context(browser: Browser, case: _Case, *, media: bool) -> BrowserContext:
    context = browser.new_context(
        locale="pt-BR", viewport={"width": case.width, "height": 900}
    )
    if media:
        context.grant_permissions(["camera", "microphone"], origin=case.base)
    return context


def _page(context: BrowserContext, errors: list[str], console: list[str]) -> Page:
    page = context.new_page()
    page.set_default_timeout(20_000)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            console.append(message.text) if message.type == "error" else None
        ),
    )
    return page


def _seed_template(case: _Case) -> str:
    template = str(uuid4())
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.ehr_specialtytemplate "
            "(id,organization_id,clinic_id,key,version,title,prompts,created_at) "
            "VALUES (%s,%s,%s,%s,1,'Teleconsulta geral',%s,now())",
            [
                template,
                case.staff["organization"],
                case.staff["clinic_a"],
                template,
                Jsonb(
                    dict(
                        zip(
                            FIELDS,
                            (
                                "Relato do paciente",
                                "Achados observados por vídeo",
                                "Avaliação do médico",
                                "Plano registrado pelo médico",
                            ),
                            strict=True,
                        )
                    )
                ),
            ],
        )
    return template


def _fail_room(case: _Case, session_id: str) -> None:
    """Mark the stored room operation failed: a synthetic provider outage."""
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        conn.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET status='failed', last_error='synthetic provider outage' "
            "WHERE id=%s",
            [_room_operation(case.staff, session_id)],
        )


def _provision(physician: Page, case: _Case, data: dict[str, str]) -> str:
    _open_encounter(physician, case.base, case.staff, case.day, data["appointment"])
    physician.goto(case.staff_url)
    session_id = _create_session(physician, case.staff_url)
    _worker(_room_operation(case.staff, session_id), "sent", case.root)
    return session_id


def _htmx(page: Page, selector: str, status: int = OK) -> None:
    """Click one htmx action and require the partial response status."""
    with page.expect_response(
        lambda r: (
            r.request.method == "POST" and r.request.headers.get("hx-request") == "true"
        )
    ) as response:
        page.locator(selector).click()
    assert response.value.status == status


def _enter(physician: Page, case: _Case, session_id: str, name: str) -> None:
    """Join from the session list and land in the workspace for this patient."""
    physician.goto(case.staff_url)
    with physician.expect_navigation():
        physician.locator(f'[data-session="{session_id}"] button[value="join"]').click()
    assert physician.url == case.staff_url
    root = physician.locator("[data-teleconsult='clinician']")
    expect(root).to_have_attribute("data-session", session_id)
    expect(physician.locator("h1")).to_have_text(name)
    panel = physician.locator("#room-panel")
    expect(panel).to_have_attribute("data-role", "physician")
    expect(physician.locator("#room-name")).to_contain_text(f"tc-{session_id}")
    expect(panel).to_have_attribute("data-media", "ready")
    expect(panel).to_have_attribute("data-connection", "connected")
    assert physician.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}
    body = physician.content()
    assert 'name="referrer" content="same-origin"' in body
    assert 'name="robots" content="noindex, nofollow"' in body
    assert name not in physician.title()


def _show(physician: Page, case: _Case, panel: str) -> None:
    """Switch panels on small screens; on wide screens both stay visible."""
    other = "notes" if panel == "video" else "video"
    if case.wide:
        expect(physician.locator("[data-switch]")).to_be_hidden()
        expect(physician.locator(f'[data-panel="{panel}"]')).to_be_visible()
        expect(physician.locator(f'[data-panel="{other}"]')).to_be_visible()
        return
    physician.locator(f'[data-tab="{panel}"]').click()
    expect(physician.locator(f'[data-tab="{panel}"]')).to_have_attribute(
        "aria-selected", "true"
    )
    expect(physician.locator(f'[data-panel="{panel}"]')).to_be_visible()
    expect(physician.locator(f'[data-panel="{other}"]')).to_be_hidden()
    expect(physician.locator(f'[data-panel="{panel}"]')).to_have_attribute(
        "role", "tabpanel"
    )


def _keyboard_switch(physician: Page, case: _Case) -> None:
    """Arrow keys move between the two tabs with a visible focus ring."""
    physician.locator("#tab-video").focus()
    physician.keyboard.press("ArrowRight")
    expect(physician.locator("#tab-notes")).to_be_focused()
    expect(physician.locator("#tab-notes")).to_have_attribute("aria-selected", "true")
    expect(physician.locator("#notes-panel")).to_be_visible()
    expect(physician.locator("#room-panel")).to_be_hidden()
    ring = physician.evaluate(FOCUS_RING_JS)
    assert ring["id"] == "tab-notes"
    assert ring["outline"] != "none"
    assert ring["width"] >= 2.0
    assert ring["height"] >= 44.0
    _capture(physician, case, "keyboard-tab")
    physician.keyboard.press("ArrowLeft")
    expect(physician.locator("#tab-video")).to_be_focused()
    expect(physician.locator("#room-panel")).to_be_visible()


def _create_draft(physician: Page, case: _Case, template: str) -> str:
    _show(physician, case, "notes")
    physician.locator("#template-id").select_option(template)
    _htmx(physician, 'button[value="note-template"]')
    notes = physician.locator("#notes-panel")
    expect(notes).to_have_attribute("data-state", "draft")
    expect(notes).to_have_attribute("data-template-version", "1")
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "saved")
    version = notes.get_attribute("data-version")
    assert version is not None
    _capture(physician, case, "draft")
    return version


def _fill(physician: Page, content: dict[str, str]) -> None:
    for field, value in content.items():
        physician.locator(f"#id_{field}").fill(value)
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(physician.locator("[data-finalize]")).to_be_disabled()


def _save(physician: Page, status: int = OK) -> None:
    _htmx(physician, 'button[value="note-save"]', status)


def _document(physician: Page, case: _Case, version: str) -> None:
    """Type, switch panels without losing edits, save, and keep the video."""
    _fill(physician, FIRST)
    expect(physician.locator("[data-notes-message]")).to_be_hidden()
    _capture(physician, case, "unsaved")
    _show(physician, case, "video")
    if not case.wide:
        expect(physician.locator("[data-tab-unsaved]")).to_be_visible()
        _capture(physician, case, "video-tab-unsaved")
        _keyboard_switch(physician, case)
        _show(physician, case, "notes")
    expect(physician.locator("#id_subjective")).to_have_value(FIRST["subjective"])
    assert physician.evaluate(TRACK_JS, "video") == {"enabled": True, "state": "live"}
    _save(physician)
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "saved")
    expect(physician.locator("[data-notes-message]")).to_have_text("Rascunho salvo.")
    expect(physician.locator("#notes-panel")).to_have_attribute("data-revision", "2")
    expect(physician.locator("[data-tab-unsaved]")).to_be_hidden()
    expect(physician.locator("[data-finalize]")).to_be_enabled()
    assert stored(case.staff, version) == (2, FIRST["subjective"])
    # The camera never restarted: the same live tracks carry on.
    assert physician.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}
    expect(physician.locator("#room-panel")).to_have_attribute("data-media", "ready")
    _capture(physician, case, "saved")
    if case.wide:
        # The video panel stays in view while the notes scroll past it.
        # Scrolling to the document bottom would leave the grid entirely;
        # the check scrolls only until the notes' bottom reaches the
        # viewport, which is the travel the sticky panel must survive.
        sticky = physician.evaluate(
            """() => {
          const notes = document.querySelector('#notes-panel');
          const target = notes.getBoundingClientRect().bottom
            + window.scrollY - innerHeight;
          window.scrollTo(0, Math.max(0, target));
          const panel = document.querySelector('#room-panel');
          const box = panel.getBoundingClientRect();
          return {scrolled: window.scrollY > 0, top: box.top, bottom: box.bottom,
            position: getComputedStyle(panel).position, height: innerHeight};
        }"""
        )
        assert sticky["position"] == "sticky"
        assert sticky["scrolled"]
        # The panel is taller than the notes column, so it cannot pin at the
        # top; the invariant is that it still intersects the viewport.
        assert sticky["top"] < sticky["height"]
        assert sticky["bottom"] > 0
        physician.evaluate("() => window.scrollTo(0, 0)")


def _start_and_end_check(physician: Page, case: _Case) -> None:
    """Start the consultation from the workspace; the notes stay untouched."""
    physician.locator("#id_plan").fill(FIRST["plan"] + " (edição pendente)")
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    _show(physician, case, "video")
    _htmx(physician, 'button[value="start"]')
    expect(physician.locator("#video-session")).to_have_attribute(
        "data-state", "active"
    )
    expect(physician.locator("[data-session-badge]")).to_have_text(
        "Sessão: Em andamento"
    )
    expect(physician.locator("[data-remote-text]")).to_have_text(
        "Consulta em andamento"
    )
    expect(physician.locator("[data-patient-presence]")).to_have_text("Na sala")
    _show(physician, case, "notes")
    expect(physician.locator("#id_plan")).to_have_value(
        FIRST["plan"] + " (edição pendente)"
    )
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    _capture(physician, case, "active-unsaved")
    _save(physician)
    expect(physician.locator("#notes-panel")).to_have_attribute("data-revision", "3")


def _finalize_with_step_up(
    physician: Page, case: _Case, session_id: str, name: str, encounter: str
) -> None:
    """A stale session device denies finalize until the real challenge passes."""
    key = swap_totp_device(case.staff)
    with physician.expect_navigation(url=re.compile(r"/auth/verify/")):
        physician.locator("[data-finalize]").click()
    assert stored_versions(case.staff, encounter)[0][1] == "draft"
    _capture(physician, case, "finalize-step-up")
    token = TOTP(bytes.fromhex(key), 30, 0, 6, 0).token()
    physician.locator("#id_otp_token").fill(f"{token:06d}")
    with physician.expect_navigation():
        physician.locator("button[type=submit]").click()
    physician.wait_for_url(case.staff_url)
    _enter(physician, case, session_id, name)
    _show(physician, case, "notes")
    expect(physician.locator("#id_subjective")).to_have_value(FIRST["subjective"])
    _htmx(physician, "[data-finalize]")
    notes = physician.locator("#notes-panel")
    expect(notes).to_have_attribute("data-state", "finalized")
    expect(physician.locator("[data-finalization]")).to_have_attribute(
        "data-finalization", "local"
    )
    expect(physician.locator("[data-encounter-badge]")).to_have_text(
        "Atendimento aberto"
    )
    assert [row[1] for row in stored_versions(case.staff, encounter)] == ["finalized"]
    assert stored_encounter(case.staff, encounter) == "open"
    _capture(physician, case, "finalized")


def _amend(physician: Page, case: _Case, encounter: str) -> str:
    physician.locator("#id_reason").fill("Retificação sintética na teleconsulta")
    _htmx(physician, 'button[value="note-amend"]')
    notes = physician.locator("#notes-panel")
    expect(notes).to_have_attribute("data-state", "draft")
    expect(physician.locator("#id_subjective")).to_have_value(FIRST["subjective"])
    assert [row[1] for row in stored_versions(case.staff, encounter)] == [
        "finalized",
        "draft",
    ]
    version = notes.get_attribute("data-version")
    assert version is not None
    _capture(physician, case, "amendment-draft")
    return version


def _failed_save(physician: Page, case: _Case, version: str) -> None:
    """A real database rejection keeps the edits on screen as unsaved."""
    # ``content`` is written by every draft save, so the check rejects the
    # UPDATE itself and the stored row is provably untouched.
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "ALTER TABLE clinic_app.ehr_clinicaldocumentversion "
            "ADD CONSTRAINT task29_failure "
            "CHECK (content IS NULL) NOT VALID"
        )
    try:
        physician.locator("#id_subjective").fill("Falha sintética")
        _save(physician, UNAVAILABLE)
        expect(physician.locator("#save-state")).to_have_attribute(
            "data-state", "unsaved"
        )
        expect(physician.locator("[data-save-error]")).to_be_focused()
        expect(physician.locator("[data-save-error]")).to_contain_text(
            "não foram salvas"
        )
        expect(physician.locator("#id_subjective")).to_have_value("Falha sintética")
        expect(physician.locator("[data-finalize]")).to_be_disabled()
        assert stored(case.staff, version) == (1, FIRST["subjective"])
        _capture(physician, case, "save-failed")
    finally:
        with psycopg.connect(case.staff["dsn"]) as conn:
            conn.execute(
                "ALTER TABLE clinic_app.ehr_clinicaldocumentversion "
                "DROP CONSTRAINT task29_failure"
            )


def _lost_video(physician: Page, case: _Case, version: str) -> None:
    """Going offline keeps the edits; a save fails honestly, then succeeds."""
    physician.locator("#id_subjective").fill("Relato retificado offline")
    panel = physician.locator("#room-panel")
    physician.context.set_offline(offline=True)
    _show(physician, case, "video")
    expect(panel).to_have_attribute("data-connection", "offline")
    expect(physician.locator("[data-room-error]")).to_have_attribute(
        "data-failure", "offline"
    )
    expect(physician.locator("[data-reconnect]")).to_be_visible()
    _show(physician, case, "notes")
    expect(physician.locator("#id_subjective")).to_have_value(
        "Relato retificado offline"
    )
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    physician.locator('button[value="note-save"]').click()
    expect(physician.locator("#save-state")).to_contain_text("Sem conexão")
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(physician.locator("#save-state")).to_be_focused()
    expect(physician.locator("#id_subjective")).to_have_value(
        "Relato retificado offline"
    )
    assert stored(case.staff, version) == (1, FIRST["subjective"])
    _capture(physician, case, "offline-unsaved")
    physician.context.set_offline(offline=False)
    _show(physician, case, "video")
    expect(panel).to_have_attribute("data-connection", "connected")
    expect(physician.locator("[data-room-error]")).to_be_hidden()
    _show(physician, case, "notes")
    _save(physician)
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "saved")
    assert stored(case.staff, version) == (2, "Relato retificado offline")


def _provider_failure(
    physician: Page, case: _Case, session_id: str, version: str, encounter: str
) -> None:
    """The room fails under the physician; notes keep working, nothing closes."""
    physician.locator("#id_objective").fill("Achado durante a falha")
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    _fail_room(case, session_id)
    _show(physician, case, "video")
    physician.locator("[data-refresh-status]").click()
    panel = physician.locator("#room-panel")
    expect(panel).to_have_attribute("data-connection", "failed")
    expect(panel).to_have_attribute("data-media", "off")
    error = physician.locator("[data-room-error]")
    expect(error).to_have_attribute("data-failure", "failed")
    expect(error).to_contain_text("nada foi finalizado")
    expect(error).to_contain_text("O provedor de vídeo falhou")
    expect(physician.locator("[data-session-badge]")).to_have_text("Sessão: Falhou")
    expect(physician.locator("[data-session-actions]")).to_be_hidden()
    expect(physician.locator("[data-refresh-status]")).to_be_disabled()
    expect(physician.locator('[data-toggle="microphone"]')).to_be_disabled()
    assert physician.evaluate(TRACK_JS, "audio") is None
    _capture(physician, case, "video-failed")
    _show(physician, case, "notes")
    expect(physician.locator("#id_objective")).to_have_value("Achado durante a falha")
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(physician.locator("[data-finalization]")).to_have_count(0)
    expect(physician.locator("[data-encounter-badge]")).to_have_text(
        "Atendimento aberto"
    )
    _save(physician)
    expect(physician.locator("#notes-panel")).to_have_attribute("data-revision", "3")
    assert stored_encounter(case.staff, encounter) == "open"
    assert [row[1] for row in stored_versions(case.staff, encounter)] == [
        "finalized",
        "draft",
    ]
    _capture(physician, case, "video-failed-notes-saved")


def _leave_guard(physician: Page, case: _Case, version: str) -> None:
    """Leaving with unsaved text is intercepted; leaving anyway writes nothing."""
    physician.locator("#id_plan").fill("Plano não salvo ao sair")
    physician.locator("[data-leave-room]").click()
    assert physician.url == case.staff_url
    guard = physician.locator("[data-leave-guard]")
    expect(guard).to_be_visible()
    expect(guard).to_be_focused()
    _capture(physician, case, "leave-guard")
    physician.locator("[data-leave-stay]").click()
    expect(guard).to_be_hidden()
    expect(physician.locator("#id_subjective")).to_be_focused()
    physician.locator("[data-leave-room]").click()
    with physician.expect_navigation():
        physician.locator("[data-leave-discard]").click()
    expect(physician.locator("#teleconsult-title")).to_have_text("Teleconsulta")
    assert stored(case.staff, version) == (3, "Relato retificado offline")


def _cross_patient_write(
    physician: Page, case: _Case, second_session: str, first_version: str
) -> None:
    """A version from another encounter is denied, never written."""
    status = post_action(
        physician,
        case.staff_url,
        {
            "action": "note-save",
            "session_id": second_session,
            "version_id": first_version,
            "revision": "3",
            "subjective": "Escrita cruzada",
            "objective": "",
            "assessment": "",
            "plan": "",
        },
    )
    assert status == FORBIDDEN
    assert stored(case.staff, first_version) == (3, "Relato retificado offline")


def _reception_denied(
    reception: Page, case: _Case, session_id: str, version: str
) -> None:
    """Server-owned permissions: no physician role, no note write at all."""
    _sign_in_receptionist(reception, case.base, case.staff)
    token = next(
        cookie["value"]
        for cookie in reception.context.cookies()
        if cookie["name"] == "csrftoken"
    )
    response = reception.request.post(
        case.staff_url,
        form={
            "csrfmiddlewaretoken": token,
            "action": "note-save",
            "session_id": session_id,
            "version_id": version,
            "revision": "3",
            "subjective": "Escrita da recepção",
            "objective": "",
            "assessment": "",
            "plan": "",
        },
        max_redirects=0,
    )
    assert response.status == FORBIDDEN
    assert stored(case.staff, version) == (3, "Relato retificado offline")


def _end_video_with_unsaved(
    physician: Page, case: _Case, template: str, encounter: str
) -> str:
    """Ending the video keeps the unsaved draft and the open encounter."""
    version = _create_draft(physician, case, template)
    _fill(physician, {"subjective": "Segundo paciente, texto não salvo"})
    _show(physician, case, "video")
    _htmx(physician, 'button[value="end"]')
    facts = physician.locator("#video-session")
    expect(facts).to_have_attribute("data-state", "ended")
    notice = physician.locator("[data-session-notice]")
    expect(notice).to_be_focused()
    expect(notice).to_contain_text("não foi finalizado")
    expect(physician.locator("[data-session-badge]")).to_have_text("Sessão: Encerrada")
    expect(physician.locator("#room-panel")).to_have_attribute("data-media", "off")
    expect(physician.locator('[data-toggle="camera"]')).to_be_disabled()
    expect(physician.locator("[data-refresh-status]")).to_be_disabled()
    expect(physician.locator("[data-session-actions]")).to_have_count(0)
    assert physician.evaluate(TRACK_JS, "video") is None
    _capture(physician, case, "video-ended")
    _show(physician, case, "notes")
    expect(physician.locator("#id_subjective")).to_have_value(
        "Segundo paciente, texto não salvo"
    )
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(physician.locator("[data-finalization]")).to_have_count(0)
    assert stored_encounter(case.staff, encounter) == "open"
    assert stored(case.staff, version) == (1, "")
    _save(physician)
    assert stored(case.staff, version) == (2, "Segundo paciente, texto não salvo")
    _capture(physician, case, "video-ended-notes-saved")
    return version


def _close_with_unsaved(physician: Page, case: _Case, version: str) -> None:
    """Browser navigation with unsaved text asks first; nothing is written."""
    physician.locator("#id_subjective").fill("Texto perdido de propósito")
    seen: list[str] = []

    def accept(dialog: Dialog) -> None:
        seen.append(dialog.type)
        dialog.accept()

    physician.once("dialog", accept)
    with physician.expect_navigation():
        physician.locator(".nav-brand").click()
    assert seen == ["beforeunload"]
    assert stored(case.staff, version) == (2, "Segundo paciente, texto não salvo")


def _assert_served_script_is_local_only(page: Page, case: _Case) -> None:
    response = page.request.get(f"{case.base}/static/js/teleconsult-clinician.js")
    assert response.ok
    script = response.text()
    assert "MediaRecorder" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "indexedDB" not in script
    assert not re.search(r"https?://", script)


def _reflow(physician: Page, case: _Case, session_id: str, name: str) -> None:
    physician.set_viewport_size({"width": 320, "height": 900})
    physician.emulate_media(forced_colors="active", reduced_motion="reduce")
    _enter(physician, case, session_id, name)
    physician.locator('[data-tab="notes"]').click()
    expect(physician.locator("#notes-panel")).to_be_visible()
    _capture(physician, case, "reflow-forced-colors-notes")
    physician.locator('[data-tab="video"]').click()
    _capture(physician, case, "reflow-forced-colors-video")
    physician.emulate_media(forced_colors="none")


def _journey(  # noqa: PLR0913 - the journey needs its full context
    physician: Page,
    patient: Page,
    reception: Page,
    case: _Case,
    first: dict[str, str],
    second: dict[str, str],
    template: str,
) -> dict[str, object]:
    first_session = _provision(physician, case, first)
    second_session = _provision(physician, case, second)
    first_encounter = _encounter_of(case, first_session)
    second_encounter = _encounter_of(case, second_session)
    patient.goto(case.patient_url)
    with patient.expect_navigation():
        patient.locator(
            f'[data-session="{first_session}"] button[value="join"]'
        ).click()
    expect(patient.locator("#room-panel")).to_have_attribute("data-role", "patient")
    _enter(physician, case, first_session, first["name"])
    _capture(physician, case, "joined")
    version = _create_draft(physician, case, template)
    _document(physician, case, version)
    _start_and_end_check(physician, case)
    _finalize_with_step_up(
        physician, case, first_session, first["name"], first_encounter
    )
    amendment = _amend(physician, case, first_encounter)
    _failed_save(physician, case, amendment)
    _lost_video(physician, case, amendment)
    _provider_failure(physician, case, first_session, amendment, first_encounter)
    _leave_guard(physician, case, amendment)
    _enter(physician, case, second_session, second["name"])
    expect(physician.locator("[data-teleconsult='clinician']")).to_have_attribute(
        "data-encounter", second_encounter
    )
    _show(physician, case, "notes")
    expect(physician.locator("#template-id")).to_be_visible()
    assert "Relato retificado offline" not in physician.content()
    _capture(physician, case, "switched-patient")
    _cross_patient_write(physician, case, second_session, amendment)
    _reception_denied(reception, case, first_session, amendment)
    second_version = _end_video_with_unsaved(
        physician, case, template, second_encounter
    )
    _close_with_unsaved(physician, case, second_version)
    _assert_served_script_is_local_only(physician, case)
    return {
        "first_session": first_session,
        "second_session": second_session,
        "first_lineage": stored_versions(case.staff, first_encounter),
        "second_lineage": stored_versions(case.staff, second_encounter),
        "first_encounter_state": stored_encounter(case.staff, first_encounter),
        "second_encounter_state": stored_encounter(case.staff, second_encounter),
    }


def _encounter_of(case: _Case, session_id: str) -> str:
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        row = conn.execute(
            "SELECT encounter_id FROM clinic_app.teleconsult_teleconsultsession "
            "WHERE id=%s",
            [session_id],
        ).fetchone()
        assert row is not None
        return str(row[0])


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_clinician_video_journey(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    base = renewal_base_url
    case = _Case(
        base=base,
        staff=availability_staff,
        day=DAYS[width],
        root=renewal_artifact_root / "clinician-video",
        width=width,
        staff_url=f"{base}/teleconsult/clinics/{availability_staff['clinic_a']}/",
        patient_url=f"{base}/patient/teleconsult/",
    )
    case.root.mkdir(exist_ok=True)
    errors: list[str] = []
    console: list[str] = []
    first = _seed_patient(case, 9, NAMES[0])
    second = _seed_patient(case, 11, NAMES[1])
    template = _seed_template(case)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=os.environ["CLINIC_RENEWAL_BROWSER_EXECUTABLE"],
            args=list(MEDIA_ARGS),
        )
        contexts = [
            _context(browser, case, media=True),
            _context(browser, case, media=False),
            _context(browser, case, media=True),
            _context(browser, case, media=True),
            _context(browser, case, media=False),
        ]
        physician, admin, patient, second_patient, reception = [
            _page(context, errors, console) for context in contexts
        ]
        try:
            _sign_in_physician(physician, base, case.staff)
            sign_in_manager(admin, base, case.staff, seed_manager(case.staff))
            _publish_consent(admin, f"{base}/clinics/{case.staff['clinic_a']}/consent/")
            _redeem(patient, base, case.staff["clinic_a"], first["code"])
            _accept_consent(patient, base)
            _redeem(second_patient, base, case.staff["clinic_a"], second["code"])
            _accept_consent(second_patient, base)
            report = _journey(
                physician, patient, reception, case, first, second, template
            )
            if width == 375:
                third = _seed_patient(case, 14, "Paciente Sintético Três")
                contexts.append(_context(browser, case, media=False))
                third_patient = _page(contexts[-1], errors, console)
                _redeem(third_patient, base, case.staff["clinic_a"], third["code"])
                _accept_consent(third_patient, base)
                third_session = _provision(physician, case, third)
                _reflow(physician, case, third_session, third["name"])
            assert not errors, errors
            # The browser logs the two deliberate failures: the rejected save
            # (503) and the offline save (htmx sendError plus its afterRequest).
            unexpected = [
                message
                for message in console
                if not message.startswith(("htmx:sendError", "htmx:afterRequest"))
                and "net::ERR_INTERNET_DISCONNECTED" not in message
                and "status of 503" not in message
            ]
            assert len([m for m in console if "status of 503" in m]) == 1
            assert len([m for m in console if m.startswith("htmx:sendError")]) == 1
            assert not unexpected, unexpected
            (case.root / f"accessibility-console-{width}.json").write_text(
                json.dumps(
                    {
                        "page_errors": errors,
                        "console_errors": unexpected,
                        "expected_console": [m for m in console if m not in unexpected],
                        "media": "chromium --use-fake-device-for-media-stream",
                        "offline": "BrowserContext.set_offline",
                        "failed_save_http": UNAVAILABLE,
                        "cross_patient_http": FORBIDDEN,
                        "reception_http": FORBIDDEN,
                        "step_up": "device swap -> /auth/verify/ -> rejoin -> finalize",
                        "real_provider": "waiting_external",
                        **report,
                    },
                    indent=2,
                    default=str,
                )
                + "\n"
            )
        finally:
            for context in contexts:
                context.close()
            browser.close()


def _hold_save(physician: Page, case: _Case, late: str, status: int) -> None:
    """Save, then type more before the real response reaches the page.

    The save request is intercepted at the browser transport, completed
    against the real server, and released only after the later text has been
    typed: the response/edit order is fixed by the interception, not timed.
    """
    seen: list[int] = []

    def hold(route: Route) -> None:
        if "action=note-save" not in (route.request.post_data or ""):
            route.continue_()
            return
        response = route.fetch()
        seen.append(response.status)
        physician.locator("#id_subjective").fill(late)
        route.fulfill(response=response)

    physician.route(case.staff_url, hold)
    try:
        _save(physician, status)
    finally:
        physician.unroute(case.staff_url, hold)
    assert seen == [status]


def _settled_unsaved_tone(physician: Page) -> None:
    """After htmx settles the swapped panel, the restored line still reads as unsaved.

    htmx re-applies the server's class attribute to same-id elements shortly
    after the swap; waiting for the panel to leave its settling state (not for
    time) makes the tone check meaningful.
    """
    expect(physician.locator("#notes-panel")).not_to_have_class(
        re.compile(r"\bhtmx-settling\b")
    )
    expect(physician.locator("#save-state")).to_have_class(
        re.compile(r"\bfeedback--error\b")
    )


def _save_race(physician: Page, case: _Case, version: str) -> None:
    """A successful save never erases text typed while its response was in flight."""
    physician.locator("#id_subjective").fill(SENT)
    _hold_save(physician, case, LATE, OK)
    notes = physician.locator("#notes-panel")
    # The swapped panel carries the stored revision; only then are the fields final.
    expect(notes).to_have_attribute("data-revision", "2")
    assert stored(case.staff, version) == (2, SENT)
    expect(physician.locator("#id_subjective")).to_have_value(LATE)
    line = physician.locator("#save-state")
    expect(line).to_have_attribute("data-state", "unsaved")
    expect(line).to_have_attribute("data-late-edits", "true")
    expect(physician.locator("[data-notes-message]")).to_be_hidden()
    expect(physician.locator("[data-finalize]")).to_be_disabled()
    _settled_unsaved_tone(physician)
    _capture(physician, case, "save-race")
    _save(physician)
    expect(line).to_have_attribute("data-state", "saved")
    expect(notes).to_have_attribute("data-revision", "3")
    assert stored(case.staff, version) == (3, LATE)


def _failed_save_race(physician: Page, case: _Case, version: str) -> None:
    """A failure response that replaces the panel keeps the later text as well."""
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "ALTER TABLE clinic_app.ehr_clinicaldocumentversion "
            "ADD CONSTRAINT task29_race "
            "CHECK (content IS NULL) NOT VALID"
        )
    try:
        physician.locator("#id_subjective").fill(REJECTED)
        _hold_save(physician, case, REJECTED_LATE, UNAVAILABLE)
        expect(physician.locator("[data-save-error]")).to_be_visible()
        expect(physician.locator("#id_subjective")).to_have_value(REJECTED_LATE)
        line = physician.locator("#save-state")
        expect(line).to_have_attribute("data-state", "unsaved")
        expect(line).to_have_attribute("data-late-edits", "true")
        expect(physician.locator("[data-finalize]")).to_be_disabled()
        _settled_unsaved_tone(physician)
        assert stored(case.staff, version) == (3, LATE)
        _capture(physician, case, "save-race-failed")
    finally:
        with psycopg.connect(case.staff["dsn"]) as conn:
            conn.execute(
                "ALTER TABLE clinic_app.ehr_clinicaldocumentversion "
                "DROP CONSTRAINT task29_race"
            )
    _save(physician)
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "saved")
    assert stored(case.staff, version) == (4, REJECTED_LATE)


def _viewed(case: _Case, version: str) -> int:
    """Count the tenant ledger's clinical-read events for exactly this version."""
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        row = conn.execute(
            "SELECT count(*) FROM clinic_app.audit_event "
            "WHERE event_type='ehr.record.viewed' AND affected_record_id=%s",
            [version],
        ).fetchone()
        assert row is not None
        return int(row[0])


def _audited_display(
    physician: Page, case: _Case, session_id: str, name: str, version: str
) -> None:
    """Showing the draft again is a clinical read: one viewed event for it."""
    before = _viewed(case, version)
    _enter(physician, case, session_id, name)
    _show(physician, case, "notes")
    expect(physician.locator("#notes-panel")).to_have_attribute("data-version", version)
    expect(physician.locator("#id_subjective")).to_have_value(REJECTED_LATE)
    assert _viewed(case, version) == before + 1
    _capture(physician, case, "audited-display")


def _record_url(case: _Case) -> str:
    return f"{case.base}/ehr/clinics/{case.staff['clinic_a']}/encounter/"


def _expect_record(page: Page, name: str, absent: str) -> None:
    """The full record shows exactly the named patient."""
    expect(page.locator("#encounter-title")).to_have_text("Atendimento")
    expect(page.locator("h2", has_text=name)).to_be_visible()
    assert absent not in page.content()


def _guarded_record(
    physician: Page, case: _Case, version: str, first: dict[str, str], second: str
) -> None:
    """Unsaved text guards the record exit; leaving anyway keeps the chosen action.

    The record the exit opens displays the draft's saved content, so that
    display is a clinical read of exactly that version: one more
    ``ehr.record.viewed`` for it, appended by the record response itself.
    """
    before = _viewed(case, version)
    physician.locator("#id_plan").fill(PENDING_PLAN)
    physician.locator("[data-open-record]").click()
    guard = physician.locator("[data-leave-guard]")
    expect(guard).to_be_visible()
    expect(guard).to_be_focused()
    assert physician.url == case.staff_url
    physician.locator("[data-leave-stay]").click()
    expect(guard).to_be_hidden()
    expect(physician.locator("#id_subjective")).to_be_focused()
    physician.locator("[data-open-record]").click()
    expect(guard).to_be_visible()
    with physician.expect_navigation(url=_record_url(case)):
        physician.locator("[data-leave-discard]").click()
    _expect_record(physician, first["name"], second)
    expect(physician.locator("#id_subjective")).to_have_value(REJECTED_LATE)
    assert PENDING_PLAN not in physician.content()
    assert stored(case.staff, version) == (4, REJECTED_LATE)
    assert _viewed(case, version) == before + 1
    _capture(physician, case, "record-discard")


def _interleaved_record(
    physician: Page, other: Page, case: _Case, first: str, second: str
) -> None:
    """Two tabs' record actions, one held across the other, each show their own patient.

    Tab A's record POST is held at the browser transport after the real server
    has answered it; tab B then opens patient two's record for real; only then
    is A's unchanged response released. A redirect resolving the shared
    selection would now land on patient two, so the response must be the
    record itself (200, not 302) and must show patient one.
    """
    record_url = _record_url(case)
    held: list[int] = []

    def hold(route: Route) -> None:
        if "action=show" not in (route.request.post_data or ""):
            route.continue_()
            return
        response = route.fetch(max_redirects=0)
        held.append(response.status)
        with other.expect_navigation(url=record_url):
            other.locator("[data-open-record]").click()
        _expect_record(other, second, first)
        route.fulfill(response=response)

    physician.route(record_url, hold)
    try:
        with physician.expect_navigation(url=record_url):
            physician.locator("[data-open-record]").click()
    finally:
        physician.unroute(record_url, hold)
    assert held == [OK]
    _expect_record(physician, first, second)
    _capture(physician, case, "record-context")


def _record_context(  # noqa: PLR0913 - two tabs, two patients, one case
    physician: Page,
    other: Page,
    case: _Case,
    sessions: tuple[str, str],
    version: str,
    first: dict[str, str],
    second: dict[str, str],
) -> None:
    """The full record opens for the displayed patient, whatever another tab does."""
    first_session, second_session = sessions
    _enter(other, case, second_session, second["name"])
    expect(physician.locator("h1")).to_have_text(first["name"])
    _guarded_record(physician, case, version, first, second["name"])
    _enter(physician, case, first_session, first["name"])
    _show(physician, case, "notes")
    physician.locator("#id_plan").fill(PENDING_PLAN)
    _save(physician)
    expect(physician.locator("#save-state")).to_have_attribute("data-state", "saved")
    _interleaved_record(physician, other, case, first["name"], second["name"])
    # A forged binding is denied; nothing renders for it.
    assert (
        post_action(
            physician,
            _record_url(case),
            {"action": "show", "encounter_id": str(uuid4())},
        )
        == FORBIDDEN
    )


def _record_denied(reception: Page, case: _Case, encounter: str) -> None:
    """Server-owned binding: without the assigned physician role nothing opens."""
    _sign_in_receptionist(reception, case.base, case.staff)
    assert (
        post_action(
            reception, _record_url(case), {"action": "show", "encounter_id": encounter}
        )
        == FORBIDDEN
    )


def _native_transitions(  # noqa: PLR0913 - the native check needs its full context
    native: Page,
    case: _Case,
    session_id: str,
    name: str,
    template: str,
    encounter: str,
) -> None:
    """Without JavaScript, start and end carry the typed draft back as unsaved."""
    native.goto(case.staff_url)
    with native.expect_navigation():
        native.locator(f'[data-session="{session_id}"] button[value="join"]').click()
    expect(native.locator("h1")).to_have_text(name)
    native.locator("#template-id").select_option(template)
    with native.expect_navigation():
        native.locator('button[value="note-template"]').click()
    notes = native.locator("#notes-panel")
    expect(notes).to_have_attribute("data-state", "draft")
    version = notes.get_attribute("data-version")
    assert version is not None
    expect(native.locator('button[value="start"]')).to_have_attribute(
        "form", "soap-form"
    )
    native.locator("#id_subjective").fill(NATIVE["subjective"])
    with native.expect_navigation():
        native.locator('button[value="start"]').click()
    expect(native.locator("#video-session")).to_have_attribute("data-state", "active")
    expect(native.locator("#id_subjective")).to_have_value(NATIVE["subjective"])
    expect(native.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    assert stored(case.staff, version) == (1, "")
    native.locator("#id_objective").fill(NATIVE["objective"])
    with native.expect_navigation():
        native.locator('button[value="end"]').click()
    expect(native.locator("#video-session")).to_have_attribute("data-state", "ended")
    expect(native.locator("[data-session-notice]")).to_be_visible()
    expect(native.locator("#id_subjective")).to_have_value(NATIVE["subjective"])
    expect(native.locator("#id_objective")).to_have_value(NATIVE["objective"])
    expect(native.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(native.locator("[data-finalization]")).to_have_count(0)
    assert stored(case.staff, version) == (1, "")
    assert stored_encounter(case.staff, encounter) == "open"
    _capture(native, case, "native-end-unsaved")
    with native.expect_navigation():
        native.locator('button[value="note-save"]').click()
    expect(native.locator("#save-state")).to_have_attribute("data-state", "saved")
    assert stored(case.staff, version) == (2, NATIVE["subjective"])
    _capture(native, case, "native-end-saved")


def test_clinician_video_preservation(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """Edits, patient context and audit hold across the boundaries the journey skips.

    A save response arriving after more typing (success and database failure),
    the full-record action from a tab whose sibling joined another patient and
    opens its own record while the first action is in flight, the guarded
    record exit taken anyway, the clinical-read audit of the displayed draft,
    and a native start/end without JavaScript carrying the typed text.
    """
    base = renewal_base_url
    case = _Case(
        base=base,
        staff=availability_staff,
        day=PRESERVATION_DAY,
        root=renewal_artifact_root / "clinician-video",
        width=1280,
        staff_url=f"{base}/teleconsult/clinics/{availability_staff['clinic_a']}/",
        patient_url=f"{base}/patient/teleconsult/",
    )
    case.root.mkdir(exist_ok=True)
    errors: list[str] = []
    console: list[str] = []
    first = _seed_patient(case, 9, NAMES[0])
    second = _seed_patient(case, 11, NAMES[1])
    template = _seed_template(case)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=os.environ["CLINIC_RENEWAL_BROWSER_EXECUTABLE"],
            args=list(MEDIA_ARGS),
        )
        contexts = [
            _context(browser, case, media=True),
            _context(browser, case, media=False),
            _context(browser, case, media=False),
            _context(browser, case, media=False),
            _context(browser, case, media=False),
        ]
        physician, admin, patient, second_patient, reception = [
            _page(context, errors, console) for context in contexts
        ]
        try:
            _sign_in_physician(physician, base, case.staff)
            sign_in_manager(admin, base, case.staff, seed_manager(case.staff))
            _publish_consent(admin, f"{base}/clinics/{case.staff['clinic_a']}/consent/")
            _redeem(patient, base, case.staff["clinic_a"], first["code"])
            _accept_consent(patient, base)
            _redeem(second_patient, base, case.staff["clinic_a"], second["code"])
            _accept_consent(second_patient, base)
            first_session = _provision(physician, case, first)
            second_session = _provision(physician, case, second)
            first_encounter = _encounter_of(case, first_session)
            second_encounter = _encounter_of(case, second_session)
            _enter(physician, case, first_session, first["name"])
            version = _create_draft(physician, case, template)
            _save_race(physician, case, version)
            _failed_save_race(physician, case, version)
            _audited_display(physician, case, first_session, first["name"], version)
            other = _page(contexts[0], errors, console)
            _record_context(
                physician,
                other,
                case,
                (first_session, second_session),
                version,
                first,
                second,
            )
            _record_denied(reception, case, first_encounter)
            # The native check signs the physician in again (a fresh authenticator
            # binding), so the JavaScript workspace and its polling close first.
            contexts[0].close()
            contexts.append(
                browser.new_context(
                    locale="pt-BR",
                    viewport={"width": case.width, "height": 900},
                    java_script_enabled=False,
                )
            )
            native = _page(contexts[-1], errors, console)
            _sign_in_physician(native, base, case.staff)
            _native_transitions(
                native, case, second_session, second["name"], template, second_encounter
            )
            assert not errors, errors
            # The raced, database-rejected save is the one deliberate 503; the
            # browser may log that and nothing else.
            unexpected = [m for m in console if "status of 503" not in m]
            assert not unexpected, unexpected
        finally:
            for context in contexts:
                context.close()
            browser.close()
