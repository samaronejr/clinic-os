"""Patient waiting room and room surface with real synthetic media devices.

The suite launches its own runner-selected engine with synthetic media
devices (``engines.install_media``: Chromium's fake capture devices behind its
real permission model; on Firefox/WebKit, live synthetic tracks behind a
context-level permission). Permission is granted per context and denied by
leaving it ungranted. Connection loss is the browser's real offline emulation, session
expiry is a stored-row change and the ended room is the physician's real end
action. No provider SDK exists; the surface is the synthetic capability slice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
import pytest
from playwright.sync_api import expect, sync_playwright

from renewal.browser._page_wait import wait_for_js
from renewal.browser.engines import (
    END_TRACK_JS,
    displays_clipped_video,
    grant_media,
    install_media,
    launch_selected,
    media_source,
    watch_page_errors,
)
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_retention import post_action, seed_manager, sign_in_manager
from renewal.browser.test_teleconsult import (
    DAYS,
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

    from playwright.sync_api import Browser, BrowserContext, Page

__all__ = ("availability_staff",)
TIMEOUT_MS = 20_000
FORBIDDEN = 403
CONFLICT = 409
PATIENT_PATH = "/patient/teleconsult/"
TRACK_JS = """(kind) => {
  const video = document.querySelector('[data-self-video]');
  const stream = video && video.srcObject;
  if (!stream) return null;
  const tracks = kind === 'audio' ? stream.getAudioTracks() : stream.getVideoTracks();
  if (!tracks.length) return null;
  return {enabled: tracks[0].enabled, state: tracks[0].readyState};
}"""
# Holds the next getUserMedia open until the test releases it, then answers
# with the real synthetic stream so a late resolution is observable.
# Patched on the prototype: WebKit ignores an own-property override on the
# navigator.mediaDevices instance.
STALL_MEDIA_JS = """() => {
  const proto = MediaDevices.prototype;
  const real = proto.getUserMedia;
  window.__lateMedia = {release: null, stream: null};
  proto.getUserMedia = function (constraints) {
    return new Promise((resolve, reject) => {
      window.__lateMedia.release = () => real.call(this, constraints).then((stream) => {
        window.__lateMedia.stream = stream;
        resolve(stream);
      }, reject);
    });
  };
}"""
# The same hold from document start: every getUserMedia stays open, as while
# the patient has not answered the browser's camera/microphone prompt yet.
PENDING_PROMPT_JS = f"({STALL_MEDIA_JS})()"
PLAYING_CAMERA_JS = """(selector) => {
  const video = document.querySelector(selector);
  const stream = video && video.srcObject;
  const track = stream && stream.getVideoTracks()[0];
  return Boolean(track) && track.readyState === 'live' && !video.paused;
}"""
LATE_TRACKS_JS = """() => {
  const stream = window.__lateMedia.stream;
  return stream ? stream.getTracks().map((track) => track.readyState) : null;
}"""
FOCUS_RING_JS = """() => {
  const el = document.activeElement;
  const style = getComputedStyle(el);
  return {tag: el.tagName, text: el.textContent.trim(), outline: style.outlineStyle,
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


def _capture(page: Page, case: _Case, state: str) -> None:
    page.screenshot(path=str(case.root / f"{state}-{case.width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _context(browser: Browser, case: _Case, *, media: bool) -> BrowserContext:
    context = browser.new_context(
        locale="pt-BR", viewport={"width": case.width, "height": 900}
    )
    install_media(context, case.base, granted=media)
    return context


def _page(context: BrowserContext, errors: list[str], console: list[str]) -> Page:
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    watch_page_errors(page, errors)
    page.on(
        "console",
        lambda message: (
            console.append(message.text) if message.type == "error" else None
        ),
    )
    return page


def _provision(physician: Page, case: _Case, data: dict[str, str]) -> str:
    """Open the encounter, create the session and provision its room."""
    _open_encounter(physician, case.base, case.staff, case.day, data["appointment"])
    physician.goto(case.staff_url)
    session_id = _create_session(physician, case.staff_url)
    _worker(_room_operation(case.staff, session_id), "sent", case.root)
    return session_id


def _staff_action(physician: Page, case: _Case, session_id: str, action: str) -> None:
    physician.goto(case.staff_url)
    with physician.expect_navigation():
        physician.locator(
            f'[data-session="{session_id}"] button[value="{action}"]'
        ).click()


def _expire_patient_session(case: _Case, patient_id: str) -> None:
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        conn.execute(
            "UPDATE clinic_app.intake_patientsession "
            "SET created_at=now()-interval '9 hours', "
            "expires_at=now()-interval '1 minute' WHERE patient_id=%s",
            [patient_id],
        )


def _assert_private(page: Page, case: _Case, session_id: str) -> None:
    """The room leaks nothing: no secrets in URL, storage, cookies or metadata."""
    assert page.url == case.patient_url
    assert session_id not in page.url
    body = page.content()
    assert "Paciente Sintético" not in body
    assert 'name="referrer" content="same-origin"' in body
    assert 'name="robots" content="noindex, nofollow"' in body
    storage = page.evaluate(
        "() => ({local: Object.entries(localStorage),"
        " session: Object.entries(sessionStorage), cookie: document.cookie})"
    )
    assert storage["local"] == []
    # The shared shell's htmx keeps only the current path in session storage.
    assert [key for key, _ in storage["session"]] in (
        [],
        ["htmx-current-path-for-history"],
    )
    for value in [storage["cookie"], *[value for _, value in storage["session"]]]:
        assert session_id not in value
        assert "tc-" not in value


def _assert_status_request(case: _Case, requests: list[dict[str, str]]) -> None:
    """Status polls carry the session only in the body, referer same-origin."""
    assert requests
    for request in requests:
        assert request["url"] == case.patient_url
        assert request["referer"] == case.patient_url
        assert 'name="action"' in request["body"]
        assert 'name="session_id"' in request["body"]


def _camera_frame(page: Page, selector: str) -> None:
    """The synthetic camera reaches the clipped video element.

    HAVE_CURRENT_DATA (a painted first frame) wherever the engine displays
    clipped video; on WebKit, which never does (engines.displays_clipped_video),
    the element must be playing a live camera track.
    """
    if displays_clipped_video(page.context):
        wait_for_js(
            page, "(s) => document.querySelector(s).readyState >= 2", arg=selector
        )
    else:
        wait_for_js(page, PLAYING_CAMERA_JS, arg=selector)


def _check_devices(patient: Page, case: _Case) -> None:
    """Explicit device test: idle, ready with a live preview, then released."""
    panel = patient.locator("[data-device-check]")
    expect(panel).to_have_attribute("data-device-state", "idle")
    expect(patient.locator("[data-consent]")).to_contain_text(
        "Consentimento de teleconsulta registrado"
    )
    _capture(patient, case, "waiting-idle")
    patient.locator("[data-device-test]").click()
    expect(panel).to_have_attribute("data-device-state", "ready")
    expect(patient.locator('[data-device="camera"]')).to_have_text("Pronta")
    expect(patient.locator('[data-device="microphone"]')).to_have_text("Pronto")
    expect(patient.locator("[data-device-status]")).to_be_focused()
    expect(patient.locator("[data-preview-video]")).to_be_visible()
    _camera_frame(patient, "[data-preview-video]")
    _capture(patient, case, "waiting-ready")
    patient.locator("[data-device-stop]").click()
    expect(panel).to_have_attribute("data-device-state", "idle")
    assert patient.evaluate(
        "() => document.querySelector('[data-preview-video]').srcObject === null"
    )


def _enter_room(patient: Page, case: _Case, session_id: str) -> None:
    with patient.expect_navigation():
        patient.locator('button[value="join"]').click()
    panel = patient.locator("#room-panel")
    expect(panel).to_have_attribute("data-role", "patient")
    expect(patient.locator("#room-name")).to_contain_text(f"tc-{session_id}")
    expect(panel).to_have_attribute("data-media", "ready")
    expect(panel).to_have_attribute("data-connection", "connected")
    expect(patient.locator("[data-connection-status]")).to_contain_text(
        "Aguardando o médico"
    )
    assert patient.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}
    assert patient.evaluate(TRACK_JS, "video") == {"enabled": True, "state": "live"}
    _camera_frame(patient, "[data-self-video]")
    _capture(patient, case, "room-connected")


def _keyboard_controls(patient: Page, case: _Case) -> None:
    """Mic and camera toggle from the keyboard with visible focus and state."""
    mic = patient.locator('[data-toggle="microphone"]')
    camera = patient.locator('[data-toggle="camera"]')
    mic.focus()
    patient.keyboard.press("Space")
    expect(mic).to_have_attribute("aria-pressed", "false")
    expect(mic).to_have_text("Microfone desligado")
    assert patient.evaluate(TRACK_JS, "audio") == {"enabled": False, "state": "live"}
    ring = patient.evaluate(FOCUS_RING_JS)
    assert ring["text"] == "Microfone desligado"
    assert ring["outline"] != "none"
    assert ring["width"] >= 2.0
    assert ring["height"] >= 44.0
    patient.keyboard.press("Tab")
    expect(camera).to_be_focused()
    patient.keyboard.press("Space")
    expect(camera).to_have_attribute("aria-pressed", "false")
    expect(camera).to_have_text("Câmera desligada")
    expect(patient.locator("[data-self-off]")).to_have_text("Câmera desligada")
    assert patient.evaluate(TRACK_JS, "video") == {"enabled": False, "state": "live"}
    _capture(patient, case, "room-muted")
    patient.keyboard.press("Tab")
    expect(patient.locator("[data-leave]")).to_be_focused()
    patient.keyboard.press("Shift+Tab")
    patient.keyboard.press("Space")
    expect(camera).to_have_attribute("aria-pressed", "true")
    patient.keyboard.press("Shift+Tab")
    patient.keyboard.press("Space")
    expect(mic).to_have_attribute("aria-pressed", "true")
    assert patient.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}


def _drop_connection(patient: Page, case: _Case) -> None:
    """Real offline emulation shows the recovery and reconnects when back."""
    panel = patient.locator("#room-panel")
    patient.context.set_offline(offline=True)
    expect(panel).to_have_attribute("data-connection", "offline")
    error = patient.locator("[data-room-error]")
    expect(error).to_have_attribute("data-failure", "offline")
    expect(error).to_be_focused()
    expect(patient.locator("[data-reconnect]")).to_be_visible()
    _capture(patient, case, "room-offline")
    patient.locator("[data-reconnect]").click()
    expect(panel).to_have_attribute("data-connection", "offline")
    patient.context.set_offline(offline=False)
    expect(panel).to_have_attribute("data-connection", "connected")
    expect(error).to_be_hidden()
    assert patient.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}


def _lose_device(patient: Page) -> None:
    """A track that ends mid-call is reported and can be re-acquired."""
    panel = patient.locator("#room-panel")
    patient.evaluate(END_TRACK_JS)
    expect(panel).to_have_attribute("data-media", "lost")
    expect(patient.locator("[data-room-error]")).to_have_attribute(
        "data-failure", "lost"
    )
    expect(patient.locator('[data-toggle="microphone"]')).to_be_disabled()
    patient.locator("[data-retry-media]").click()
    expect(panel).to_have_attribute("data-media", "ready")
    expect(patient.locator("[data-room-error]")).to_be_hidden()


def _stall_media_retry(patient: Page) -> None:
    """Lose a device, then retry with the acquisition held open by the test."""
    panel = patient.locator("#room-panel")
    patient.evaluate(END_TRACK_JS)
    expect(panel).to_have_attribute("data-media", "lost")
    patient.evaluate(STALL_MEDIA_JS)
    patient.locator("[data-retry-media]").click()
    expect(panel).to_have_attribute("data-media", "pending")
    wait_for_js(patient, "() => window.__lateMedia.release !== null")


def _assert_late_media_dropped(patient: Page, state: str) -> None:
    """An acquisition resolving after a terminal state is stopped, not installed."""
    panel = patient.locator("#room-panel")
    expect(panel).to_have_attribute("data-connection", state)
    expect(panel).to_have_attribute("data-media", "off")
    patient.evaluate("() => window.__lateMedia.release()")
    wait_for_js(
        patient,
        "() => { const s = window.__lateMedia.stream;"
        " return s !== null && s.getTracks().every((t) => t.readyState === 'ended'); }",
    )
    assert patient.evaluate(LATE_TRACKS_JS) == ["ended", "ended"]
    expect(panel).to_have_attribute("data-media", "off")
    expect(panel).to_have_attribute("data-connection", state)
    expect(patient.locator("[data-room-error]")).to_have_attribute(
        "data-failure", state
    )
    expect(patient.locator("[data-self]")).to_be_hidden()
    expect(patient.locator('[data-toggle="microphone"]')).to_be_disabled()
    expect(patient.locator('[data-toggle="camera"]')).to_be_disabled()
    assert patient.evaluate(TRACK_JS, "audio") is None
    assert patient.evaluate(TRACK_JS, "video") is None


def _ended_room(patient: Page, physician: Page, case: _Case, session_id: str) -> None:
    """The physician's end shows up on the next explicit check; nothing reopens."""
    panel = patient.locator("#room-panel")
    _stall_media_retry(patient)
    _staff_action(physician, case, session_id, "end")
    patient.locator("[data-refresh-status]").click()
    expect(panel).to_have_attribute("data-connection", "ended")
    expect(panel).to_have_attribute("data-state", "ended")
    expect(patient.locator("[data-state-badge]")).to_have_text("Consulta encerrada")
    error = patient.locator("[data-room-error]")
    expect(error).to_have_attribute("data-failure", "ended")
    expect(patient.locator("[data-back]")).to_be_visible()
    expect(patient.locator('[data-toggle="microphone"]')).to_be_disabled()
    expect(patient.locator("[data-refresh-status]")).to_be_disabled()
    assert patient.evaluate(TRACK_JS, "audio") is None
    _assert_late_media_dropped(patient, "ended")
    _capture(patient, case, "room-ended")
    with patient.expect_navigation():
        patient.locator("[data-back]").click()
    expect(patient.locator("h1")).to_have_text("Sala de espera")
    expect(patient.locator("#empty-title")).to_be_visible()
    row = patient.locator(f'.tele-history-item[data-session="{session_id}"]')
    expect(row).to_have_attribute("data-state", "ended")
    expect(row).to_contain_text("não pode ser reaberta")
    row.locator("summary").click()
    expect(row.locator('[data-event="ended"]')).to_contain_text("Consulta encerrada")
    _capture(patient, case, "waiting-ended")
    assert (
        post_action(
            patient, case.patient_url, {"action": "join", "session_id": session_id}
        )
        == CONFLICT
    )


def _happy_path(
    physician: Page, patient: Page, case: _Case, session_id: str
) -> list[dict[str, str]]:
    """Waiting room, device test, join, controls, drop, loss, active, ended."""
    requests: list[dict[str, str]] = []
    patient.on(
        "request",
        lambda request: (
            requests.append(
                {
                    "url": request.url,
                    "referer": request.headers.get("referer", ""),
                    "body": request.post_data or "",
                }
            )
            if request.method == "POST" and request.resource_type == "fetch"
            else None
        ),
    )
    patient.goto(case.patient_url)
    expect(patient.locator("h1")).to_have_text("Sala de espera")
    expect(patient.locator(f'[data-session="{session_id}"]')).to_have_attribute(
        "data-state", "waiting"
    )
    _check_devices(patient, case)
    _enter_room(patient, case, session_id)
    _assert_private(patient, case, session_id)
    _keyboard_controls(patient, case)
    _drop_connection(patient, case)
    _lose_device(patient)
    _staff_action(physician, case, session_id, "join")
    _staff_action(physician, case, session_id, "start")
    patient.locator("[data-refresh-status]").click()
    expect(patient.locator("#room-panel")).to_have_attribute("data-state", "active")
    expect(patient.locator("[data-state-badge]")).to_have_text("Consulta em andamento")
    expect(patient.locator("[data-physician]")).to_have_text("Já está na sala")
    expect(patient.locator("[data-remote-text]")).to_have_text("Consulta em andamento")
    _capture(patient, case, "room-active")
    _ended_room(patient, physician, case, session_id)
    _assert_status_request(case, requests)
    return requests


def _reconnect_while_denied(denied: Page, case: _Case) -> None:
    """Losing the connection while denied must not lose the device guidance."""
    room = denied.locator("#room-panel")
    error = denied.locator("[data-room-error]")
    denied.context.set_offline(offline=True)
    expect(room).to_have_attribute("data-connection", "offline")
    expect(error).to_have_attribute("data-failure", "offline")
    expect(denied.locator("[data-reconnect]")).to_be_visible()
    expect(denied.locator("[data-retry-media]")).to_be_hidden()
    denied.context.set_offline(offline=False)
    expect(room).to_have_attribute("data-connection", "connected")
    expect(room).to_have_attribute("data-media", "denied")
    expect(error).to_have_attribute("data-failure", "denied")
    expect(error).to_be_focused()
    expect(error).to_contain_text("cadeado")
    expect(denied.locator("[data-retry-media]")).to_be_visible()
    expect(denied.locator("[data-reconnect]")).to_be_hidden()
    expect(denied.locator("[data-connection-status]")).to_contain_text("Conectado")
    _capture(denied, case, "room-denied-reconnected")


def _denied_path(denied: Page, case: _Case, session_id: str, patient_id: str) -> None:
    """Denied devices are explained, recoverable, then the session expires."""
    denied.goto(case.patient_url)
    panel = denied.locator("[data-device-check]")
    denied.locator("[data-device-test]").click()
    expect(panel).to_have_attribute("data-device-state", "denied")
    error = denied.locator("[data-device-error]")
    expect(error).to_have_attribute("data-failure", "denied")
    expect(error).to_be_focused()
    expect(error).to_contain_text("Permissão negada")
    expect(error).to_contain_text("cadeado")
    expect(denied.locator('[data-device="camera"]')).to_have_text("Permissão negada")
    expect(denied.locator('button[value="join"]')).to_be_enabled()
    _capture(denied, case, "waiting-denied")
    with denied.expect_navigation():
        denied.locator('button[value="join"]').click()
    room = denied.locator("#room-panel")
    expect(room).to_have_attribute("data-media", "denied")
    expect(room).to_have_attribute("data-connection", "connected")
    expect(denied.locator("[data-room-error]")).to_have_attribute(
        "data-failure", "denied"
    )
    expect(denied.locator('[data-toggle="microphone"]')).to_have_text(
        "Microfone indisponível"
    )
    expect(denied.locator("[data-retry-media]")).to_be_visible()
    _capture(denied, case, "room-denied")
    _reconnect_while_denied(denied, case)
    # The patient fixes the browser permission and retries without leaving.
    room_error = denied.locator("[data-room-error]")
    grant_media(denied.context, case.base)
    denied.locator("[data-retry-media]").click()
    expect(room).to_have_attribute("data-media", "ready")
    expect(room_error).to_be_hidden()
    assert denied.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}
    _capture(denied, case, "room-recovered")
    _stall_media_retry(denied)
    _expire_patient_session(case, patient_id)
    denied.locator("[data-refresh-status]").click()
    expect(room).to_have_attribute("data-connection", "expired")
    expect(room_error).to_have_attribute("data-failure", "expired")
    expect(denied.locator("[data-portal]")).to_be_visible()
    assert denied.evaluate(TRACK_JS, "audio") is None
    _assert_late_media_dropped(denied, "expired")
    _capture(denied, case, "room-expired")
    with denied.expect_navigation():
        denied.locator("[data-portal]").click()
    expect(denied.locator("h1")).to_have_text("Acesso necessário")
    _capture(denied, case, "gate-expired")
    assert (
        post_action(
            denied, case.patient_url, {"action": "join", "session_id": session_id}
        )
        == FORBIDDEN
    )


def _unanswered_prompt(patient: Page, case: _Case, session_id: str) -> None:
    """The room connects and follows the session while the prompt is open."""
    patient.goto(case.patient_url)
    with patient.expect_navigation():
        patient.locator(
            f'form:has(input[name="session_id"][value="{session_id}"]) '
            'button[value="join"]'
        ).click()
    panel = patient.locator("#room-panel")
    expect(panel).to_have_attribute("data-connection", "connected")
    expect(panel).to_have_attribute("data-media", "pending")
    expect(patient.locator("[data-connection-status]")).to_contain_text(
        "Aguardando o médico"
    )
    # Answering the prompt later brings the devices into the same room.
    wait_for_js(patient, "() => window.__lateMedia.release !== null")
    patient.evaluate("() => window.__lateMedia.release()")
    expect(panel).to_have_attribute("data-media", "ready")
    expect(panel).to_have_attribute("data-connection", "connected")
    assert patient.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}
    _capture(patient, case, "room-prompt-answered")


def _assert_served_script_is_local_only(patient: Page, case: _Case) -> None:
    """The room script records nothing and loads no provider SDK."""
    response = patient.request.get(f"{case.base}/static/js/teleconsult-patient.js")
    assert response.ok
    script = response.text()
    assert "MediaRecorder" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "indexedDB" not in script
    assert not re.search(r"https?://", script)


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_patient_video_journey(
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
        root=renewal_artifact_root / "patient-video",
        width=width,
        staff_url=f"{base}/teleconsult/clinics/{availability_staff['clinic_a']}/",
        patient_url=f"{base}{PATIENT_PATH}",
    )
    case.root.mkdir(exist_ok=True)
    errors: list[str] = []
    console: list[str] = []
    data = _seed(case.staff, case.day, 9)
    second = _seed(case.staff, case.day, 11)
    third = _seed(case.staff, case.day, 13)
    with sync_playwright() as driver:
        browser = launch_selected(driver, media=True)
        contexts = [
            _context(browser, case, media=False),
            _context(browser, case, media=False),
            _context(browser, case, media=True),
            _context(browser, case, media=False),
            _context(browser, case, media=True),
        ]
        contexts[4].add_init_script(PENDING_PROMPT_JS)
        physician, admin, patient, denied, prompted = [
            _page(context, errors, console) for context in contexts
        ]
        try:
            _sign_in_physician(physician, base, case.staff)
            sign_in_manager(admin, base, case.staff, seed_manager(case.staff))
            _publish_consent(admin, f"{base}/clinics/{case.staff['clinic_a']}/consent/")
            _redeem(patient, base, case.staff["clinic_a"], data["code"])
            _accept_consent(patient, base)
            session_id = _provision(physician, case, data)
            requests = _happy_path(physician, patient, case, session_id)
            _assert_served_script_is_local_only(patient, case)
            _redeem(denied, base, case.staff["clinic_a"], second["code"])
            _accept_consent(denied, base)
            second_session = _provision(physician, case, second)
            _denied_path(denied, case, second_session, second["patient"])
            _redeem(prompted, base, case.staff["clinic_a"], third["code"])
            _accept_consent(prompted, base)
            _unanswered_prompt(prompted, case, _provision(physician, case, third))
            if width == 375:
                patient.set_viewport_size({"width": 320, "height": 900})
                patient.emulate_media(forced_colors="active", reduced_motion="reduce")
                patient.goto(case.patient_url)
                _capture(patient, case, "waiting-reflow-forced-colors")
            assert not errors, errors
            unexpected = [
                message
                for message in console
                if "status of 409" not in message and "status of 403" not in message
            ]
            assert not unexpected, unexpected
            (case.root / f"accessibility-console-{width}.json").write_text(
                json.dumps(
                    {
                        "page_errors": errors,
                        "console_errors": unexpected,
                        "status_polls": len(requests),
                        "ended_join_status": CONFLICT,
                        "expired_join_status": FORBIDDEN,
                        "media": media_source(patient.context),
                        "permissions": "granted per context; denied by omission",
                        "offline": "BrowserContext.set_offline",
                        "real_provider": "waiting_external",
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            for context in contexts:
                context.close()
            browser.close()
