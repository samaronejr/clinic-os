"""Two-persona synthetic recovery on the real clinic_app HTTP surface.

Local fake-device tracks are real browser media, not a remote provider call.
Transport interception holds actual server responses to order failure events;
no sleeps, provider credentials, recording or clinical-data traces are used.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import psycopg
import pytest
from playwright.sync_api import expect, sync_playwright

from renewal.browser.engines import (
    grant_media,
    install_media,
    launch_selected,
    media_source,
    watch_page_errors,
)
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_clinician_video import (
    FIRST,
    _Case,
    _create_draft,
    _fill,
    _htmx,
    _save,
    _seed_template,
    _show,
)
from renewal.browser.test_encounter import stored
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_patient_video import TRACK_JS
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

    from playwright.sync_api import APIResponse, Page, Route

__all__ = ("availability_staff",)
DAYS = {1280: "2035-09-03", 768: "2035-09-04", 375: "2035-09-05"}
# The MessageChannel event is a task-queue barrier after the response JSON's
# promise consumers, not a timed delay. It proves the held reply was consumed
# before testing that it could not replace the newer terminal state.
OBSERVE_STATUS = """() => {
  const real = window.fetch.bind(window);
  window.fetch = (...args) => {
    const watched = window.__watchNextStatus === true;
    window.__watchNextStatus = false;
    return real(...args).then(response => {
      if (watched) {
        const json = response.json.bind(response);
        response.json = () => json().then(value => {
          const channel = new MessageChannel();
          channel.port1.onmessage = () => {
            channel.port1.close(); channel.port2.close();
            console.info('task30-held-status-consumed');
          };
          channel.port2.postMessage(null);
          return value;
        });
      }
      return response;
    });
  };
}"""


def capture(page: Page, case: _Case, state: str) -> None:
    page.screenshot(path=str(case.root / f"{state}-{case.width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def state_rows(case: _Case, session: str) -> dict[str, object]:
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        row = conn.execute(
            "SELECT s.state,s.revision,e.state,"
            "r.recording_enabled,r.transcription_enabled "
            "FROM clinic_app.teleconsult_teleconsultsession s "
            "JOIN clinic_app.ehr_encounter e ON e.id=s.encounter_id "
            "JOIN clinic_app.teleconsult_teleconsultroom r ON r.session_id=s.id "
            "WHERE s.id=%s",
            [session],
        ).fetchone()
        assert row is not None
        events = conn.execute(
            "SELECT kind,actor_role FROM clinic_app.teleconsult_teleconsultevent "
            "WHERE session_id=%s ORDER BY created_at,id",
            [session],
        ).fetchall()
        live = conn.execute(
            "SELECT count(*) FROM clinic_app.teleconsult_teleconsultcredential c "
            "JOIN clinic_app.teleconsult_teleconsultsession s ON s.id=c.session_id "
            "WHERE session_id=%s AND revoked_at IS NULL AND expires_at>now() "
            "AND s.state IN ('waiting','active')",
            [session],
        ).fetchone()
        assert live is not None
    return {
        "session": row[0],
        "revision": row[1],
        "encounter": row[2],
        "recording": row[3],
        "transcription": row[4],
        "events": events,
        "live_credentials": live[0],
    }


def status_request(route: Route) -> bool:
    return (
        route.request.method == "POST"
        and route.request.headers.get("accept") == "application/json"
    )


def refresh(page: Page) -> None:
    with page.expect_response(
        lambda r: (
            r.request.method == "POST"
            and r.request.headers.get("accept") == "application/json"
        )
    ) as response:
        page.locator("[data-refresh-status]").click()
    assert response.value.status == 200


def transport_recovery(page: Page, case: _Case, persona: str) -> None:
    panel = page.locator("#room-panel")
    page.context.set_offline(offline=True)
    expect(panel).to_have_attribute("data-connection", "offline")
    expect(page.locator("[data-room-error]")).to_be_focused()
    capture(page, case, f"{persona}-disconnected")
    page.context.set_offline(offline=False)
    expect(panel).to_have_attribute("data-connection", "connected")
    # Fail the exact next status request as a transport timeout, then explicitly
    # recover against the real server. No synthetic success response is used.
    url = case.patient_url if persona == "patient" else case.staff_url
    injected: list[str] = []

    def timeout(route: Route) -> None:
        if status_request(route) and not injected:
            injected.append("timedout")
            route.abort("timedout")
        else:
            route.continue_()

    page.route(url, timeout)
    try:
        with page.expect_event(
            "requestfailed", predicate=lambda request: request.url == url
        ):
            page.locator("[data-refresh-status]").click()
        expect(panel).to_have_attribute("data-connection", "offline")
        capture(page, case, f"{persona}-timeout")
    finally:
        page.unroute(url, timeout)
    assert injected == ["timedout"]
    with page.expect_response(
        lambda r: r.request.headers.get("accept") == "application/json"
    ):
        page.locator("[data-reconnect]").click()
    expect(panel).to_have_attribute("data-connection", "connected")
    assert page.evaluate(TRACK_JS, "audio") == {"enabled": True, "state": "live"}


def end_with_late_status(physician: Page, patient: Page, case: _Case) -> None:
    """Actual active replies arrive after the stored end on both personas."""
    held: dict[str, tuple[Route, APIResponse]] = {}
    pages = {"physician": physician, "patient": patient}
    intercepted: set[str] = set()

    def hold(persona: str, route: Route) -> None:
        if status_request(route) and persona not in intercepted:
            intercepted.add(persona)
            response = route.fetch()
            assert response.json()["state"] == "active"
            held[persona] = (route, response)
            pages[persona].evaluate("() => console.info('task30-status-held')")
        else:
            route.continue_()

    physician.route(case.staff_url, lambda route: hold("physician", route))
    patient.route(case.patient_url, lambda route: hold("patient", route))
    try:
        for page in pages.values():
            page.evaluate(OBSERVE_STATUS)
            page.evaluate("() => {window.__watchNextStatus = true;}")
            with page.expect_console_message(
                predicate=lambda m: m.text == "task30-status-held"
            ):
                page.locator("[data-refresh-status]").click()
        assert set(held) == set(pages)
        _htmx(physician, 'button[value="end"]')
        expect(physician.locator("#room-panel")).to_have_attribute(
            "data-connection", "ended"
        )
        refresh(patient)
        expect(patient.locator("#room-panel")).to_have_attribute(
            "data-connection", "ended"
        )
        for persona, page in pages.items():
            route, response = held.pop(persona)
            with page.expect_console_message(
                predicate=lambda m: m.text == "task30-held-status-consumed"
            ):
                route.fulfill(response=response)
            expect(page.locator("#room-panel")).to_have_attribute(
                "data-connection", "ended"
            )
            expect(page.locator("#room-panel")).to_have_attribute("data-state", "ended")
            expect(page.locator("#room-panel")).to_have_attribute("data-media", "off")
            assert page.evaluate(TRACK_JS, "audio") is None
            capture(page, case, f"{persona}-ended-late-reply")
    finally:
        for route, _ in held.values():
            route.abort()
        for page in pages.values():
            page.unroute_all(behavior="wait")


def preserved(case: _Case, version: str, session: str) -> None:
    assert stored(case.staff, version) == (2, FIRST["subjective"])
    rows = state_rows(case, session)
    assert rows["encounter"] == "open"
    assert rows["recording"] is False
    assert rows["transcription"] is False


def device_recovery(patient: Page, case: _Case) -> None:
    """Browser-enforced permission denial followed by an explicit grant/retry."""
    patient.locator("[data-device-test]").click()
    expect(patient.locator("[data-device-check]")).to_have_attribute(
        "data-device-state", "denied"
    )
    expect(patient.locator("[data-device-error]")).to_be_focused()
    capture(patient, case, "permission-denied")
    grant_media(patient.context, case.base)
    patient.locator("[data-device-test]").click()
    expect(patient.locator("[data-device-check]")).to_have_attribute(
        "data-device-state", "ready"
    )
    capture(patient, case, "devices-ready")


def begin(
    physician: Page, patient: Page, case: _Case, data: dict[str, str]
) -> tuple[str, str]:
    _redeem(patient, case.base, case.staff["clinic_a"], data["code"])
    capture(patient, case, "invitation-redeemed")
    _accept_consent(patient, case.base)
    capture(patient, case, "consent-accepted")
    _open_encounter(physician, case.base, case.staff, case.day, data["appointment"])
    physician.goto(case.staff_url)
    session = _create_session(physician, case.staff_url)
    _worker(_room_operation(case.staff, session), "sent", case.root)
    template = _seed_template(case)
    patient.goto(case.patient_url)
    device_recovery(patient, case)
    with patient.expect_navigation():
        patient.locator('button[value="join"]').click()
    physician.goto(case.staff_url)
    with physician.expect_navigation():
        physician.locator(f'[data-session="{session}"] button[value="join"]').click()
    for page in (patient, physician):
        expect(page.locator("#room-panel")).to_have_attribute(
            "data-connection", "connected"
        )
        expect(page.locator("#room-panel")).to_have_attribute("data-media", "ready")
        for kind in ("audio", "video"):
            assert page.evaluate(TRACK_JS, kind) == {"enabled": True, "state": "live"}
    capture(patient, case, "patient-connected")
    version = _create_draft(physician, case, template)
    _fill(physician, FIRST)
    _save(physician)
    expect(physician.locator("#notes-panel")).to_have_attribute("data-revision", "2")
    _show(physician, case, "video")
    _htmx(physician, 'button[value="start"]')
    expect(physician.locator("#video-session")).to_have_attribute(
        "data-state", "active"
    )
    refresh(patient)
    expect(patient.locator("#room-panel")).to_have_attribute("data-state", "active")
    capture(physician, case, "notes-saved-active")
    return session, version


def outage_and_expiry(
    physician: Page, patient: Page, case: _Case, session: str
) -> None:
    """Revoke portal access and fail the stored synthetic provider operation."""
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        conn.execute(
            "UPDATE clinic_app.intake_patientsession "
            "SET created_at=now()-interval '9 hours', "
            "expires_at=now()-interval '1 minute' "
            "WHERE patient_id=(SELECT patient_id "
            "FROM clinic_app.teleconsult_teleconsultsession WHERE id=%s)",
            [session],
        )
    with patient.expect_response(lambda r: r.status == 403):
        patient.locator("[data-refresh-status]").click()
    expect(patient.locator("#room-panel")).to_have_attribute(
        "data-connection", "expired"
    )
    assert patient.evaluate(TRACK_JS, "audio") is None
    capture(patient, case, "access-expired")
    assert (
        post_action(
            patient, case.patient_url, {"action": "join", "session_id": session}
        )
        == 403
    )
    with psycopg.connect(case.staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [case.staff["organization"]],
        )
        conn.execute(
            "UPDATE clinic_app.comms_integrationoperation "
            "SET status='failed' WHERE id=%s",
            [_room_operation(case.staff, session)],
        )
    refresh(physician)
    expect(physician.locator("#room-panel")).to_have_attribute(
        "data-connection", "failed"
    )
    assert physician.evaluate(TRACK_JS, "audio") is None
    capture(physician, case, "provider-outage")
    assert (
        post_action(
            physician, case.staff_url, {"action": "join", "session_id": session}
        )
        == 409
    )


def journey(
    physician: Page, patient: Page, case: _Case, data: dict[str, str], ending: str
) -> dict[str, object]:
    session, version = begin(physician, patient, case, data)
    transport_recovery(patient, case, "patient")
    transport_recovery(physician, case, "physician")
    preserved(case, version, session)
    if ending == "outage":
        outage_and_expiry(physician, patient, case, session)
        _show(physician, case, "notes")
        expect(physician.locator("#id_subjective")).to_have_value(FIRST["subjective"])
        capture(physician, case, "notes-preserved-outage")
        preserved(case, version, session)
        rows = state_rows(case, session)
        assert rows["session"] == "failed"
        assert rows["live_credentials"] == 0
        assert rows["events"] == [
            ("created", ""),
            ("joined", "patient"),
            ("joined", "physician"),
            ("started", "physician"),
            ("failed", ""),
        ]
        return rows
    end_with_late_status(physician, patient, case)
    # Duplicate end and delayed start/join are real HTTP requests, not mocked.
    assert (
        post_action(physician, case.staff_url, {"action": "end", "session_id": session})
        == 302
    )
    assert (
        post_action(
            physician, case.staff_url, {"action": "start", "session_id": session}
        )
        == 409
    )
    assert (
        post_action(
            patient, case.patient_url, {"action": "join", "session_id": session}
        )
        == 409
    )
    assert (
        post_action(patient, case.patient_url, {"action": "end", "session_id": session})
        == 403
    )
    _show(physician, case, "notes")
    expect(physician.locator("#id_subjective")).to_have_value(FIRST["subjective"])
    expect(physician.locator("#notes-panel")).to_have_attribute("data-state", "draft")
    capture(physician, case, "notes-preserved-terminal")
    preserved(case, version, session)
    rows = state_rows(case, session)
    assert rows["session"] == "ended"
    assert rows["live_credentials"] == 0
    assert rows["events"] == [
        ("created", ""),
        ("joined", "patient"),
        ("joined", "physician"),
        ("started", "physician"),
        ("ended", "physician"),
    ]
    return rows


@pytest.mark.parametrize(
    ("width", "ending"), [(1280, "end"), (768, "end"), (375, "end"), (1280, "outage")]
)
def test_video_recovery_journey(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
    ending: str,
) -> None:
    staff, base = availability_staff, renewal_base_url
    root = renewal_artifact_root / "video-recovery"
    if ending == "outage":
        root /= "outage"
    root.mkdir(parents=True, exist_ok=True)
    case = _Case(
        base,
        staff,
        DAYS[width] if ending == "end" else "2035-09-06",
        root,
        width,
        f"{base}/teleconsult/clinics/{staff['clinic_a']}/",
        f"{base}/patient/teleconsult/",
    )
    errors: list[str] = []
    console: list[str] = []
    with sync_playwright() as driver:
        browser = launch_selected(driver, media=True)
        contexts = [
            # Routed requests: WebKit's route() misses service-worker-controlled
            # pages (engines.py, Request interception).
            browser.new_context(
                locale="pt-BR",
                viewport={"width": width, "height": 900},
                service_workers="block",
            )
            for _ in range(3)
        ]
        # Only the physician starts with devices; the patient is denied until
        # device_recovery grants them.
        for index, context in enumerate(contexts):
            install_media(context, base, granted=index == 0)
        physician, patient, manager = [context.new_page() for context in contexts]
        for page in (physician, patient, manager):
            page.set_default_timeout(20_000)
            watch_page_errors(page, errors)
            page.on(
                "console",
                lambda message: (
                    console.append(message.text) if message.type == "error" else None
                ),
            )
        try:
            _sign_in_physician(physician, base, staff)
            sign_in_manager(manager, base, staff, seed_manager(staff))
            _publish_consent(manager, f"{base}/clinics/{staff['clinic_a']}/consent/")
            rows = journey(physician, patient, case, _seed(staff, case.day, 12), ending)
            assert not errors
            unexpected = [
                message
                for message in console
                if not any(
                    code in message
                    for code in ("ERR_TIMED_OUT", "409 (Conflict)", "403 (Forbidden)")
                )
            ]
            assert not unexpected
            (root / f"capability-{width}.json").write_text(
                json.dumps(
                    {
                        "mode": "synthetic",
                        "provider_backed": False,
                        "remote_peer_media": False,
                        "browser": browser.browser_type.name,
                        "version": browser.version,
                        "width": width,
                        "devices": f"{media_source(patient.context)}; "
                        "permission denial then grant",
                        "transport": [
                            "offline/reconnect both personas",
                            "status request timedout both personas",
                            "actual active replies delivered after end"
                            if ending == "end"
                            else "portal expiry and stored provider outage",
                        ],
                        "state": rows,
                        "page_errors": errors,
                        "console_errors_expected": console,
                        "provider_sandbox": "waiting_external",
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            for context in contexts:
                context.close()
            browser.close()
