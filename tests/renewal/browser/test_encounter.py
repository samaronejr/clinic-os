"""Real clinic_app proof: SOAP saves, stale edits, denial and storage failure."""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect
from psycopg.types.json import Jsonb

from renewal.browser._page_wait import click_when_hittable, evaluate_js
from renewal.browser._protected import decrypt
from renewal.browser.engines import element_box, full_page_screenshot, new_context
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_questionnaires import _seed as seed_patient

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page, Response, Route

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
        # Lifecycle v2 (D-9): encounters open only after arrival; the booked
        # physician (appointment.move_own, own schedule) records it.
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [staff["physician_a_id"]],
        )
        conn.execute(
            "UPDATE clinic_app.scheduling_appointment SET status='arrived' WHERE id=%s",
            [data["appointment"]],
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
    full_page_screenshot(page, folder / f"{state}-{width}.png")
    assert evaluate_js(page, "document.documentElement.scrollWidth <= innerWidth")


def press(page: Page, action: str) -> None:
    with page.expect_navigation():
        page.locator(f'button[value="{action}"]').first.click()


def press_in_view(page: Page, action: str) -> None:
    """``press`` for a button far outside the viewport (see click_when_hittable)."""
    with page.expect_navigation():
        click_when_hittable(page.locator(f'button[value="{action}"]').first)


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
    # Plan item 27: a stale save now opens the conflict compare (nothing written).
    expect(second.locator("#save-state")).to_have_attribute("data-state", "conflict")
    expect(second.locator("#conflict-panel")).to_be_visible()
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
        target = element_box(fallback.locator('button[value="save"]'))
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


# ---------------------------------------------------------------------------
# Plan item 27: server autosave, a dropped save, offline retry, two tabs.
# ---------------------------------------------------------------------------

AUTOSAVE = "/api/ui/v1/ehr/autosave/"
AUTOSAVE_DAY = "2035-06-05"
SAVED_TEXT = re.compile(r"Salvo às \d\d:\d\d \(revisão (\d+)\)")
# Every save-state change is recorded in the page (never storage) so the test
# can prove no "saved" state appeared between a dropped save and its replay.
RECORD_STATES_JS = """() => {
  const node = document.getElementById('save-state');
  window.__saveStates = [];
  new MutationObserver(() => window.__saveStates.push(node.dataset.state))
    .observe(node, {attributes: true, childList: true, characterData: true,
                    subtree: true});
}"""


def stored_soap(staff: dict[str, str], version: str) -> tuple[int, dict[str, str]]:
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
        plaintext = decrypt(conn, "ehr.clinicaldocumentversion.content", bytes(row[1]))
        assert plaintext is not None
        return row[0], dict(json.loads(plaintext))


def receipts(staff: dict[str, str], command: str) -> int:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT count(*) FROM clinic_app.ehr_draftsavereceipt WHERE command_id=%s",
            [command],
        ).fetchone()
        assert row is not None
        return int(row[0])


def is_autosave(response: Response) -> bool:
    return response.url.endswith(AUTOSAVE) and response.request.method == "POST"


def paused(page: Page) -> None:
    """Hold the page's timers; only ``fast_forward`` fires the debounce."""
    page.clock.pause_at(int(time.time() * 1000) + 1000)


def autosave_after_pause(page: Page) -> Response:
    with page.expect_response(is_autosave) as response:
        page.clock.fast_forward(1500)
    return response.value


@dataclass
class AutosaveScene:
    staff: dict[str, str]
    root: Path
    url: str
    version: str
    page: Page
    bodies: list[object]
    errors: list[str]


def saved_revision(status: Locator) -> str:
    match = SAVED_TEXT.fullmatch(status.inner_text().strip())
    assert match is not None
    return match.group(1)


def happy_autosave(scene: AutosaveScene) -> None:
    """Type, pause: only the server's acknowledgement makes the state saved."""
    page, status = scene.page, scene.page.locator("#save-state")
    page.locator("#id_subjective").fill("Relato sintético autosalvo")
    expect(status).to_have_attribute("data-state", "unsaved")
    assert autosave_after_pause(page).status == 200
    expect(status).to_have_attribute("data-state", "saved")
    expect(status).to_have_text(SAVED_TEXT)
    assert saved_revision(status) == "2"
    assert stored_soap(scene.staff, scene.version)[0] == 2
    capture(page, scene.root, "autosave-saved", 1280)


def dropped_then_offline(scene: AutosaveScene) -> list[str]:
    """The save commits, its response is lost, then 30 s offline, then replay."""
    page, status = scene.page, scene.page.locator("#save-state")
    page.evaluate(RECORD_STATES_JS)
    first_retry = len(scene.bodies)
    dropped: list[int] = []

    def drop(route: Route) -> None:
        response = route.fetch()
        dropped.append(response.status)
        route.abort("failed")

    pattern = re.compile(re.escape(AUTOSAVE) + "$")
    page.route(pattern, drop)
    page.locator("#id_subjective").fill("Relato após a queda da rede")
    with page.expect_event("requestfailed"):
        page.clock.fast_forward(1500)
    page.unroute(pattern)
    assert dropped == [200]
    expect(status).to_have_attribute("data-state", "retrying")
    expect(status).to_have_text("Não salvo - tentando novamente")
    revision, content = stored_soap(scene.staff, scene.version)
    assert (revision, content["subjective"]) == (3, "Relato após a queda da rede")
    capture(page, scene.root, "autosave-retrying", 1280)
    page.context.set_offline(offline=True)
    page.clock.fast_forward(30000)
    expect(status).to_have_attribute("data-state", "retrying")
    capture(page, scene.root, "autosave-offline-30s", 1280)
    with page.expect_response(is_autosave) as replay:
        page.context.set_offline(offline=False)
        page.clock.fast_forward(30000)
    assert replay.value.status == 200
    expect(status).to_have_text(SAVED_TEXT)
    assert saved_revision(status) == "3"
    commands = {
        str(body["editor_command_id"])
        for body in scene.bodies[first_retry:]
        if isinstance(body, dict)
    }
    assert len(commands) == 1
    assert receipts(scene.staff, commands.pop()) == 1
    assert stored_soap(scene.staff, scene.version)[0] == 3
    states = [str(state) for state in page.evaluate("window.__saveStates")]
    assert "retrying" in states
    assert "saved" not in states[:-1]
    assert states[-1] == "saved"
    capture(page, scene.root, "autosave-replayed", 1280)
    return states


def second_tab_compares(scene: AutosaveScene) -> Page:
    """A second tab is refused, asks for the lock, then compares and merges."""
    page, status = scene.page, scene.page.locator("#save-state")
    second = page.context.new_page()
    second.on("pageerror", lambda error: scene.errors.append(str(error)))
    second.clock.install()
    second.goto(scene.url)
    expect(second.locator("[data-revision]")).to_have_attribute("data-revision", "3")
    paused(second)
    second_status = second.locator("#save-state")
    second.locator("#id_plan").fill("Plano da segunda aba")
    assert autosave_after_pause(second).status == 423
    expect(second_status).to_have_attribute("data-state", "locked-by-other")
    expect(second.locator("#lock-panel")).to_be_visible()
    capture(second, scene.root, "autosave-locked-by-other", 1280)
    with second.expect_response(is_autosave) as asked:
        second.locator("[data-request-handover]").click()
    assert (asked.value.status, asked.value.json()["code"]) == (
        423,
        "handover_requested",
    )
    page.locator("#id_assessment").fill("Avaliação da primeira aba")
    handed = autosave_after_pause(page)
    assert (handed.status, handed.json()["lock"]) == (200, "handed_over")
    expect(status).to_have_attribute("data-state", "locked-by-other")
    expect(page.locator("#id_assessment")).to_have_attribute("readonly", "")
    capture(page, scene.root, "autosave-handed-over", 1280)
    revision, content = stored_soap(scene.staff, scene.version)
    assert revision == 4
    assert content["assessment"] == "Avaliação da primeira aba"
    assert content["plan"] != "Plano da segunda aba"
    with second.expect_response(is_autosave) as retried:
        second.clock.fast_forward(20000)
    assert retried.value.status == 409
    expect(second_status).to_have_attribute("data-state", "conflict")
    expect(second.locator("#conflict-panel")).to_be_visible()
    expect(second.locator("#conflict-plan")).to_be_visible()
    expect(second.locator("#conflict-assessment")).to_be_visible()
    assert stored_soap(scene.staff, scene.version) == (revision, content)
    capture(second, scene.root, "autosave-conflict-compare", 1280)
    second.locator('[data-use-saved="assessment"]').click()
    expect(second.locator("#id_assessment")).to_have_value("Avaliação da primeira aba")
    with second.expect_response(is_autosave) as merged:
        second.locator("[data-merge]").click()
    assert (merged.value.status, merged.value.json()["revision"]) == (200, 5)
    expect(second_status).to_have_text(SAVED_TEXT)
    expect(second.locator("#conflict-panel")).to_be_hidden()
    revision, content = stored_soap(scene.staff, scene.version)
    assert revision == 5
    assert content["plan"] == "Plano da segunda aba"
    assert content["assessment"] == "Avaliação da primeira aba"
    capture(second, scene.root, "autosave-merged", 1280)
    return second


def test_autosave_survives_a_dropped_save_offline_and_a_second_tab(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    base = renewal_base_url
    folder = renewal_artifact_root / "encounter"
    folder.mkdir(exist_ok=True, mode=0o700)
    browser = renewal_page.context.browser
    assert browser is not None
    data = seed(staff, AUTOSAVE_DAY)
    context = browser.new_context(
        locale="pt-BR",
        viewport={"width": 1280, "height": 900},
        record_video_dir=folder,
    )
    try:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.clock.install()
        _sign_in_physician(page, base, staff)
        page.goto(
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{AUTOSAVE_DAY}/1/"
        )
        press(page, "open")
        page.locator("#template-id").select_option(data["specialty"])
        press(page, "template")
        version = page.locator("[data-version]").get_attribute("data-version")
        assert version is not None
        bodies: list[object] = []
        page.on(
            "request",
            lambda request: (
                bodies.append(request.post_data_json)
                if request.url.endswith(AUTOSAVE)
                else None
            ),
        )
        paused(page)
        scene = AutosaveScene(
            staff, renewal_artifact_root, page.url, version, page, bodies, errors
        )
        happy_autosave(scene)
        states = dropped_then_offline(scene)
        second = second_tab_compares(scene)
        assert not errors
        (folder / "autosave-report.json").write_text(
            json.dumps(
                {
                    "happy_revision": 2,
                    "retry_command_ids": 1,
                    "revision_after_replay": 3,
                    "states_after_drop": states,
                    "locked_http": 423,
                    "conflict_http": 409,
                    "merged_revision": 5,
                    "page_errors": errors,
                },
                indent=2,
            )
            + "\n"
        )
        first_video, second_video = page.video, second.video
        assert first_video is not None
        assert second_video is not None
    finally:
        context.close()
    first_video.save_as(folder / "autosave.webm")
    second_video.save_as(folder / "autosave-second-tab.webm")
    first_video.delete()
    second_video.delete()
