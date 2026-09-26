"""Real clinic_app proof: finalize, amend, review, stale denial and close."""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
import pytest
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._protected import decrypt
from renewal.browser.engines import element_box
from renewal.browser.test_availability import (
    _no_overflow,
    _ring,
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_encounter import DAYS, press, seed

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

FIELDS = ("subjective", "objective", "assessment", "plan")
FIRST = {
    "subjective": "Relato sintético inicial",
    "objective": "Exame sintético inicial",
    "assessment": "Avaliação sintética inicial",
    "plan": "Plano sintético inicial",
}
SECOND = {
    "subjective": "Relato sintético retificado",
    "objective": "Exame sintético retificado",
    "assessment": "Avaliação sintética retificada",
    "plan": "Plano sintético retificado",
}
MIN_TARGET_PX = 44


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "amendments"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def csrf(page: Page) -> str:
    token = page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
    assert token
    return token


def post_action(page: Page, url: str, fields: dict[str, str]) -> tuple[int, str]:
    response = page.request.post(
        url,
        form={"csrfmiddlewaretoken": csrf(page), **fields},
        max_redirects=0,
    )
    return response.status, response.headers.get("location", "")


def stored_versions(
    staff: dict[str, str], encounter: str
) -> list[tuple[int, str, str, str]]:
    """Return (version, state, digest, subjective) with envelopes decrypted."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        rows = conn.execute(
            "SELECT v.version,v.state,coalesce(v.content_digest,''),"
            "v.content FROM clinic_app.ehr_clinicaldocumentversion v "
            "JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id "
            "WHERE d.encounter_id=%s ORDER BY v.version",
            [encounter],
        ).fetchall()
        versions: list[tuple[int, str, str, str]] = []
        for version, state, digest, envelope in rows:
            plaintext = decrypt(
                conn,
                "ehr.clinicaldocumentversion.content",
                bytes(envelope) if envelope is not None else None,
            )
            subjective = (
                str(json.loads(plaintext)["subjective"])
                if plaintext is not None
                else ""
            )
            versions.append((version, state, digest, subjective))
        return versions


def stored_encounter(staff: dict[str, str], encounter: str) -> str:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT state FROM clinic_app.ehr_encounter WHERE id=%s", [encounter]
        ).fetchone()
        assert row is not None
        return str(row[0])


def swap_totp_device(staff: dict[str, str]) -> str:
    """Replace the physician's device row; the session binding goes stale."""
    key = secrets.token_hex(20)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [staff["physician_a_id"]],
        )
        conn.execute(
            "DELETE FROM clinic_app.otp_totp_totpdevice WHERE user_id = %s",
            [staff["physician_a_id"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
            "user_id, throttling_failure_count, created_at) "
            "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
            "%s, 0, now())",
            [key, staff["physician_a_id"]],
        )
    return key


def reverify(page: Page, base: str, key: str) -> None:
    """Complete the real re-verification challenge the denial redirected to."""
    page.goto(f"{base}/auth/verify/")
    token = TOTP(bytes.fromhex(key), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    with page.expect_navigation():
        page.locator("button[type=submit]").click()


def fill_and_save(page: Page, content: dict[str, str]) -> None:
    for field, value in content.items():
        page.locator(f"#id_{field}").fill(value)
    press(page, "save")


def finalize_current(page: Page) -> None:
    """Finalize the visible draft and assert the frozen state marker."""
    press(page, "finalize")
    expect(page.locator("[data-version]")).to_have_attribute("data-state", "finalized")
    expect(page.locator("[data-finalization]")).to_have_attribute(
        "data-finalization", "local"
    )


def amend_current(page: Page, reason: str) -> None:
    """Open the linked amendment draft through the real form."""
    page.locator("#id_reason").fill(reason)
    press(page, "amend")
    expect(page.locator("[data-version]")).to_have_attribute("data-state", "draft")


def lineage(staff: dict[str, str], encounter: str) -> list[list[object]]:
    return [[row[0], row[1]] for row in stored_versions(staff, encounter)]


def open_draft(
    page: Page, staff: dict[str, str], base: str, day: str, specialty: str
) -> str:
    """Reach a saved draft through the real agenda journey."""
    _sign_in_physician(page, base, staff)
    page.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{day}/1/")
    press(page, "open")
    page.locator("#template-id").select_option(specialty)
    press(page, "template")
    fill_and_save(page, FIRST)
    version = page.locator("[data-version]").get_attribute("data-version")
    assert version is not None
    return version


def review_superseded(page: Page, root: Path, width: int, digest: str) -> None:
    """Render the preserved superseded original in the review panel."""
    with page.expect_navigation():
        page.locator('[data-version-row] button[value="review"]').last.click()
    expect(page.locator("[data-review]")).to_have_attribute("data-state", "superseded")
    expect(page.locator("[data-review]")).to_have_attribute("data-digest", digest)
    assert FIRST["subjective"] in page.content()
    capture(page, root, "review-superseded", width)
    press(page, "current")


def close_and_verify(page: Page, url: str, staff: dict[str, str]) -> str:
    """Close the encounter; a repeated POST returns the same closed row."""
    encounter = page.locator("[data-encounter]").get_attribute("data-encounter")
    assert encounter is not None
    press(page, "close")
    expect(page.locator("[data-encounter]")).to_have_attribute(
        "data-encounter-state", "closed"
    )
    assert stored_encounter(staff, encounter) == "closed"
    status, _ = post_action(page, url, {"action": "close", "encounter_id": encounter})
    assert status == 302
    return encounter


def step_up_denial(
    page: Page, url: str, staff: dict[str, str], base: str, encounter: str
) -> None:
    """A stale session device denies finalize until re-verification succeeds."""
    amend_current(page, "Retificação após encerramento")
    fill_and_save(page, SECOND)
    key = swap_totp_device(staff)
    draft_id = page.locator("[data-version]").get_attribute("data-version")
    assert draft_id is not None
    status, location = post_action(
        page,
        url,
        {"action": "finalize", "version_id": draft_id, "revision": "2"},
    )
    assert status == 302
    assert location.startswith("/auth/verify/")
    assert lineage(staff, encounter) == [
        [1, "superseded"],
        [2, "finalized"],
        [3, "draft"],
    ]
    reverify(page, base, key)
    page.goto(url)
    finalize_current(page)
    assert lineage(staff, encounter) == [
        [1, "superseded"],
        [2, "superseded"],
        [3, "finalized"],
    ]


def post_close_checks(
    page: Page, staff: dict[str, str], base: str, root: Path, width: int
) -> None:
    """Foreign denials, then the stale-save conflict recovery surface."""
    denied_read(page, staff, base, root, width)
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/encounter/"
    stale_save_conflict(page, url, root, width)


def stale_save_conflict(page: Page, url: str, root: Path, width: int) -> None:
    """A stale editor's save renders the failed edits as unsaved."""
    amend_current(page, "Retificação do conflito")
    draft_id = page.locator("[data-version]").get_attribute("data-version")
    assert draft_id is not None
    page.locator("#id_subjective").fill("Edição não salva do editor antigo")
    status, _ = post_action(
        page,
        url,
        {"action": "finalize", "version_id": draft_id, "revision": "1"},
    )
    assert status == 302
    press(page, "save")
    expect(page.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(page.locator("#soap-form")).to_have_count(0)
    expect(page.locator("[data-finalization]")).to_have_count(0)
    assert "Edição não salva do editor antigo" in page.content()
    assert SECOND["subjective"] in page.content()
    capture(page, root, "conflict-unsaved", width)


def denied_read(
    page: Page, staff: dict[str, str], base: str, root: Path, width: int
) -> None:
    """Reception and the foreign clinic get non-enumerating denials."""
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/encounter/"
    browser = page.context.browser
    assert browser is not None
    denied_context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    try:
        denied = denied_context.new_page()
        _sign_in_receptionist(denied, base, staff)
        response = denied.goto(url)
        assert response is not None
        assert response.status == 403
        assert FIRST["subjective"] not in denied.content()
        capture(denied, root, "reception-denied", width)
    finally:
        denied_context.close()
    foreign = page.request.post(
        f"{base}/ehr/clinics/{staff['clinic_b']}/encounter/",
        form={
            "csrfmiddlewaretoken": csrf(page),
            "action": "finalize",
            "version_id": str(uuid4()),
            "revision": "2",
        },
    )
    assert foreign.status == 403


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_amendment_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    data = seed(staff, DAYS[width])
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/encounter/"
    try:
        version = open_draft(page, staff, base, DAYS[width], data["specialty"])
        capture(page, root, "draft-saved", width)
        encounter = page.locator("[data-encounter]").get_attribute("data-encounter")
        assert encounter is not None

        # Finalize freezes the content and shows the local-finalization marker.
        finalize_current(page)
        digest = page.locator("[data-version]").get_attribute("data-digest")
        assert digest is not None
        assert len(digest) == 64
        capture(page, root, "finalized", width)
        rows = stored_versions(staff, encounter)
        assert lineage(staff, encounter) == [[1, "finalized"]]
        assert rows[0][2] == digest

        # A duplicate finalize POST is an idempotent redirect, not a new row.
        status, _ = post_action(
            page,
            url,
            {"action": "finalize", "version_id": version, "revision": "2"},
        )
        assert status == 302
        assert len(stored_versions(staff, encounter)) == 1

        # A stale amendment base is a conflict; the lineage stays untouched.
        amend_current(page, "Correção sintética")
        expect(page.locator("[data-version]")).to_have_attribute(
            "data-amendment-of", version
        )
        capture(page, root, "amendment-draft", width)
        for reason in ("Base antiga", "Rascunho aberto"):
            status, _ = post_action(
                page,
                url,
                {"action": "amend", "version_id": version, "reason": reason},
            )
            assert status == 409
        assert len(stored_versions(staff, encounter)) == 2

        # Finalizing the amendment supersedes the base; nothing is overwritten.
        fill_and_save(page, SECOND)
        finalize_current(page)
        rows = stored_versions(staff, encounter)
        assert lineage(staff, encounter) == [[1, "superseded"], [2, "finalized"]]
        assert rows[0][3] == FIRST["subjective"]
        assert rows[0][2] == digest  # The original digest is preserved.
        assert rows[1][2] != digest
        capture(page, root, "amended", width)

        # The review panel renders the preserved superseded original.
        review_superseded(page, root, width, digest)

        # Closing is idempotent and keeps amendments possible afterwards.
        close_and_verify(page, url, staff)
        capture(page, root, "closed", width)

        # A stale session device forces re-verification before finalizing.
        step_up_denial(page, url, staff, base, encounter)
        capture(page, root, "finalized-after-stepup", width)

        # Reception sees nothing; the foreign clinic is non-enumerating. A
        # stale editor's save then renders the failed edits as unsaved.
        post_close_checks(page, staff, base, root, width)
        assert not errors
        (root / "amendments" / f"report-{width}.json").write_text(
            json.dumps(
                {
                    "width": width,
                    "page_errors": errors,
                    "finalized_digest": digest,
                    "duplicate_finalize_status": 302,
                    "stale_amend_status": 409,
                    "open_draft_amend_status": 409,
                    "step_up_denial_location": "/auth/verify/",
                    "close_status": 302,
                    "reception_status": 403,
                    "foreign_clinic_status": 403,
                    "stale_save_unsaved_visible": True,
                    "final_lineage": lineage(staff, encounter),
                    "runtime": "clinic_app",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()


def _reflow_scene(page: Page, ctx: dict[str, Any], root: Path) -> dict[str, object]:
    """320px reflow plus keyboard order across the finalized workspace."""
    open_draft(page, ctx["staff"], ctx["base"], ctx["day"], ctx["specialty"])
    finalize_current(page)
    amend_current(page, "Correção sintética")
    fill_and_save(page, SECOND)
    finalize_current(page)
    page.set_viewport_size({"width": 320, "height": 900})
    assert _no_overflow(page)
    page.locator("#id_reason").focus()
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.tagName")
    assert focused in {"BUTTON", "A", "INPUT", "TEXTAREA", "SELECT"}
    ring = _ring(page)
    capture(page, root, "reflow-320", 320)
    return {"focus_ring": ring, "focused": focused}


def _forced_colors_scene(
    page: Page, ctx: dict[str, Any], root: Path
) -> dict[str, object]:
    """Forced-colors focus ring and zero-duration transitions."""
    open_draft(page, ctx["staff"], ctx["base"], ctx["day"], ctx["specialty"])
    finalize_current(page)
    page.locator('button[value="amend"]').focus()
    ring = _ring(page)
    assert ring["style"] == "solid"
    transition = page.evaluate(
        "getComputedStyle(document.querySelector("
        "'button[value=\"amend\"]')).transitionDuration"
    )
    assert transition == "0s"
    capture(page, root, "forced-colors", 1280)
    return {"focus_ring": ring, "transition": transition}


def _native_scene(page: Page, ctx: dict[str, Any], root: Path) -> dict[str, object]:
    """Finalize and amend with no JavaScript at 200% zoom (640 CSS px, DPR 2)."""
    open_draft(page, ctx["staff"], ctx["base"], ctx["day"], ctx["specialty"])
    finalize_current(page)
    amend_current(page, "Retificação nativa")
    target = element_box(page.locator('button[value="save"]'))
    assert target["height"] >= MIN_TARGET_PX
    capture(page, root, "native-zoom-200", 640)
    return {
        "device_pixel_ratio": page.evaluate("devicePixelRatio"),
        "save_button_height": target["height"],
        "java_script": "disabled",
    }


def test_amendment_accessibility_matrix(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """320px reflow, keyboard order, forced colors and the native fallback."""
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    # Each scene gets its own day, appointment and patient.
    ctx: dict[str, dict[str, Any]] = {
        key: {**seed(staff, day), "staff": staff, "base": base, "day": day}
        for key, day in (
            ("reflow", "2035-06-06"),
            ("colors", "2035-06-07"),
            ("native", "2035-06-08"),
        )
    }
    browser = renewal_page.context.browser
    assert browser is not None
    report: dict[str, object] = {}
    errors: list[str] = []

    def scene(options: dict[str, Any]) -> Page:
        context = browser.new_context(locale="pt-BR", **options)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        return page

    page = scene({"viewport": {"width": 1280, "height": 900}})
    try:
        report["reflow_320"] = _reflow_scene(page, ctx["reflow"], root)
    finally:
        page.context.close()
    page = scene(
        {
            "viewport": {"width": 1280, "height": 900},
            "forced_colors": "active",
            "reduced_motion": "reduce",
        }
    )
    try:
        report["forced_colors"] = _forced_colors_scene(page, ctx["colors"], root)
    finally:
        page.context.close()
    # The native fallback: no JavaScript, 200% zoom (640 CSS px at DPR 2).
    page = scene(
        {
            "viewport": {"width": 640, "height": 450},
            "device_scale_factor": 2,
            "java_script_enabled": False,
        }
    )
    try:
        report["native_zoom_200"] = _native_scene(page, ctx["native"], root)
    finally:
        page.context.close()

    assert not errors
    report["page_errors"] = errors
    destination = root / "amendments" / "accessibility-report.json"
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
