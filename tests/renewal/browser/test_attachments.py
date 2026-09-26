"""Real clinic_app proof: upload, quarantine, scan, download and denial."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser.engines import new_context
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

MATRIX_DAY = "2035-06-05"
ZOOM_WINDOW = 1280  # physical window width behind the 200% zoom scene
ZOOM_FACTOR = 2
MIN_TARGET_PX = 44
LONG_FILE_NAME = "exame-sintetico-" + "resultado-" * 12 + ".pdf"

PDF_BYTES = b"%PDF-1.4\n%synthetic browser attachment\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
ACTIVE_PDF = b"%PDF-1.4\n1 0 obj<</OpenAction<</S/JavaScript/JS(app.alert(1))>>>>\n"


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "attachments"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _attachment_ids(page: Page) -> set[str]:
    return set(
        page.locator("[data-attachment]").evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('data-attachment'))"
        )
    )


def _open_workspace(
    page: Page, staff: dict[str, str], base: str, day: str
) -> tuple[str, str]:
    """Sign in and reach the attachment workspace through the real journey."""
    _sign_in_physician(page, base, staff)
    page.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{day}/1/")
    press(page, "open")
    with page.expect_navigation():
        page.get_by_role("button", name="Anexos").click()
    expect(page.locator("#attachments-title")).to_be_visible()
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/attachments/"
    encounter = page.locator('input[name="encounter_id"]').first.input_value()
    assert encounter
    return url, encounter


def _uploaded(
    page: Page,
    name: str = "exame.pdf",
    mime: str = "application/pdf",
    data: bytes = PDF_BYTES,
) -> str:
    """Upload one file and return the new row's attachment id."""
    before = _attachment_ids(page)
    upload_file(page, name, mime, data)
    new = _attachment_ids(page) - before
    assert len(new) == 1
    attachment = new.pop()
    expect(page.locator(f'[data-attachment="{attachment}"]')).to_have_attribute(
        "data-state", "quarantined"
    )
    return attachment


def csrf(page: Page) -> str:
    token = page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
    assert token
    return token


def cookie_csrf(page: Page) -> str:
    token = next(
        cookie.get("value", "")
        for cookie in page.context.cookies()
        if cookie.get("name") == "csrftoken"
    )
    assert token
    return token


def stored_state(staff: dict[str, str], attachment: str) -> tuple[str, int]:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT state, scan_attempts FROM clinic_app.ehr_clinicalattachment "
            "WHERE id=%s",
            [attachment],
        ).fetchone()
        assert row is not None
        return row[0], row[1]


def storage_key_of(staff: dict[str, str], attachment: str) -> str:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT storage_key FROM clinic_app.ehr_clinicalattachment WHERE id=%s",
            [attachment],
        ).fetchone()
        assert row is not None
        return str(row[0])


def upload_file(
    page: Page, name: str, mime: str, data: bytes, expected: int = 302
) -> None:
    page.locator("#id_attachment").set_input_files(
        {"name": name, "mimeType": mime, "buffer": data}
    )
    with page.expect_response(lambda r: r.request.method == "POST") as response:
        press(page, "upload")
    assert response.value.status == expected


def post_download(
    page: Page, url: str, encounter: str, attachment: str
) -> tuple[int, bytes, dict[str, str]]:
    response = page.request.post(
        url,
        form={
            "csrfmiddlewaretoken": csrf(page),
            "action": "download",
            "encounter_id": encounter,
            "attachment_id": attachment,
        },
    )
    return response.status, response.body(), dict(response.headers)


def verify_download(page: Page, url: str, encounter: str, attachment: str) -> None:
    """Assert exact bytes, no-store and safe disposition on the real response."""
    status, body, headers = post_download(page, url, encounter, attachment)
    assert status == 200
    assert body == PDF_BYTES
    assert headers["content-type"] == "application/pdf"
    assert "no-store" in headers["cache-control"]
    assert headers["content-disposition"] == (
        f'attachment; filename="anexo-{attachment}.pdf"'
    )
    assert headers["x-content-type-options"] == "nosniff"
    with page.expect_download() as download:
        page.locator('button[value="download"]').first.click()
    assert download.value.path().read_bytes() == PDF_BYTES
    assert download.value.suggested_filename == f"anexo-{attachment}.pdf"


def failure_uploads(
    page: Page, url: str, encounter: str, attachment: str, root: Path
) -> None:
    """Refuse confused input at upload and active content at the scan."""
    upload_file(page, "confundido.pdf", "application/pdf", PNG_BYTES, expected=400)
    expect(page.locator(".feedback--error")).to_be_visible()
    width = page.viewport_size["width"] if page.viewport_size else 0
    capture(page, root, "type-confused-rejected", width)

    upload_file(page, "ativo.pdf", "application/pdf", ACTIVE_PDF)
    active = page.locator(
        f'[data-attachment]:not([data-attachment="{attachment}"])'
    ).get_attribute("data-attachment")
    assert active is not None
    with page.expect_response(lambda r: r.request.method == "POST"):
        page.locator(f'[data-attachment="{active}"] button[value="scan"]').click()
    expect(page.locator(f'[data-attachment="{active}"]')).to_have_attribute(
        "data-state", "rejected"
    )
    expect(
        page.locator(f'[data-attachment="{active}"] button[value="download"]')
    ).to_have_count(0)
    status, body, _ = post_download(page, url, encounter, active)
    assert status == 403
    assert ACTIVE_PDF not in body
    capture(page, root, "scan-rejected", width)


def denied_journeys(
    page: Page, staff: dict[str, str], base: str, root: Path, width: int
) -> None:
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/attachments/"
    encounter = page.locator('input[name="encounter_id"]').first.input_value()
    attachment = page.locator("[data-attachment]").first.get_attribute(
        "data-attachment"
    )
    assert attachment
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    try:
        other = context.new_page()
        _sign_in_receptionist(other, base, staff)
        response = other.goto(url)
        assert response is not None
        assert response.status == 403
        refused = other.request.post(
            url,
            form={
                "csrfmiddlewaretoken": cookie_csrf(other),
                "action": "download",
                "encounter_id": encounter,
                "attachment_id": attachment,
            },
        )
        assert refused.status == 403
        assert PDF_BYTES not in refused.body()
        capture(other, root, "reception-denied", width)
    finally:
        context.close()
    # Another clinic's URL and an altered id are non-enumerating denials.
    foreign = page.request.post(
        f"{base}/ehr/clinics/{staff['clinic_b']}/attachments/",
        form={
            "csrfmiddlewaretoken": csrf(page),
            "action": "download",
            "encounter_id": encounter,
            "attachment_id": attachment,
        },
    )
    assert foreign.status == 403
    for forged in (str(uuid4()), storage_key_of(staff, attachment)):
        status, body, _ = post_download(page, url, encounter, forged)
        assert status == 403
        assert PDF_BYTES not in body


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_attachment_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    seed(staff, DAYS[width])
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    url = f"{base}/ehr/clinics/{staff['clinic_a']}/attachments/"
    try:
        _sign_in_physician(page, base, staff)
        page.goto(
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{DAYS[width]}/1/"
        )
        press(page, "open")
        with page.expect_navigation():
            page.get_by_role("button", name="Anexos").click()
        expect(page.locator("#attachments-title")).to_be_visible()
        capture(page, root, "empty", width)
        encounter = page.locator('input[name="encounter_id"]').first.input_value()

        # A valid upload lands in quarantine; its bytes are not served yet.
        upload_file(page, "exame.pdf", "application/pdf", PDF_BYTES)
        attachment = page.locator("[data-attachment]").first.get_attribute(
            "data-attachment"
        )
        assert attachment is not None
        expect(page.locator(f'[data-attachment="{attachment}"]')).to_have_attribute(
            "data-state", "quarantined"
        )
        assert stored_state(staff, attachment) == ("quarantined", 0)
        capture(page, root, "quarantined", width)
        status, body, _ = post_download(page, url, encounter, attachment)
        assert status == 403
        assert PDF_BYTES not in body

        # The synthetic scan completes the transition; download is exact bytes.
        with page.expect_response(lambda r: r.request.method == "POST") as scanned:
            press(page, "scan")
        assert scanned.value.status == 302
        expect(page.locator(f'[data-attachment="{attachment}"]')).to_have_attribute(
            "data-state", "available"
        )
        assert stored_state(staff, attachment) == ("available", 1)
        capture(page, root, "available", width)
        verify_download(page, url, encounter, attachment)

        # Type-confused input is refused at upload; active content at the scan.
        failure_uploads(page, url, encounter, attachment, root)
        denied_journeys(page, staff, base, root, width)
        assert not errors
        (root / "attachments" / f"report-{width}.json").write_text(
            json.dumps(
                {
                    "width": width,
                    "page_errors": errors,
                    "quarantined_download_status": 403,
                    "download_status": 200,
                    "download_bytes_exact": True,
                    "download_disposition": (
                        f'attachment; filename="anexo-{attachment}.pdf"'
                    ),
                    "type_confused_status": 400,
                    "scan_rejected_download_status": 403,
                    "forged_id_status": 403,
                    "foreign_clinic_status": 403,
                    "reception_status": 403,
                    "runtime": "clinic_app",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()


def _reflow_scene(page: Page, root: Path) -> dict[str, object]:
    """320px reflow plus keyboard order and a long-content filename."""
    attachment = _uploaded(page, LONG_FILE_NAME)
    page.set_viewport_size({"width": 320, "height": 900})
    assert _no_overflow(page)
    upload_height = float(
        page.evaluate(
            "document.querySelector('#attachment-form button[type=submit]')"
            ".getBoundingClientRect().height"
        )
    )
    assert upload_height >= MIN_TARGET_PX, upload_height
    page.locator("#id_attachment").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#attachment-form button[type=submit]")).to_be_focused()
    ring = _ring(page)
    assert ring["style"] == "solid"
    capture(page, root, "reflow-keyboard", 320)
    return {
        "attachment": attachment,
        "upload_button_height": upload_height,
        "focus_ring": ring,
        "long_file_name": LONG_FILE_NAME,
    }


def _forced_colors_scene(page: Page, root: Path) -> dict[str, object]:
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    attachment = _uploaded(page)
    page.locator(f'[data-attachment="{attachment}"] button[value="scan"]').focus()
    ring = _ring(page)
    assert ring["style"] == "solid"
    capture(page, root, "forced-colors", 1280)
    return {"attachment": attachment, "focus_ring": ring}


def _reduced_motion_scene(page: Page) -> dict[str, object]:
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    transition = page.evaluate(
        "getComputedStyle(document.querySelector("
        "'#attachment-form button[type=submit]')).transitionDuration"
    )
    assert transition == "0s"
    return {"button_transition": transition}


def _zoom_200_scene(
    page: Page, url: str, encounter: str, root: Path
) -> dict[str, object]:
    """A 1280px window at 200% browser zoom: 640 CSS px at device ratio 2.

    Chromium implements page zoom as a device-scale change, so the context's
    ``device_scale_factor`` and halved viewport are the zoomed window itself.
    JavaScript is disabled to prove the native form fallback end to end.
    """
    metrics = dict(
        page.evaluate(
            "({device_pixel_ratio: devicePixelRatio, css_viewport_width:"
            " innerWidth, root_font_size:"
            " getComputedStyle(document.documentElement).fontSize})"
        )
    )
    assert metrics["device_pixel_ratio"] == ZOOM_FACTOR, metrics
    assert metrics["css_viewport_width"] == ZOOM_WINDOW // ZOOM_FACTOR, metrics
    assert _no_overflow(page)
    attachment = _uploaded(page)
    status, body, _ = post_download(page, url, encounter, attachment)
    assert status == 403
    assert PDF_BYTES not in body
    height = float(
        page.evaluate(
            "document.querySelector('#attachment-form button[type=submit]')"
            ".getBoundingClientRect().height"
        )
    )
    assert height >= MIN_TARGET_PX, height
    capture(page, root, "native-upload-zoom-200", 640)
    return {
        **metrics,
        "window_width": ZOOM_WINDOW,
        "attachment": attachment,
        "quarantined_download_status": status,
        "upload_button_height": height,
        "java_script": "disabled",
    }


def _stale_scene(
    page: Page, staff: dict[str, str], url: str, encounter: str, root: Path
) -> dict[str, object]:
    """A stale tab cannot double-transition or serve quarantined bytes."""
    attachment = _uploaded(page)
    stale = page.context.new_page()
    stale.goto(url)
    expect(stale.locator(f'[data-attachment="{attachment}"]')).to_have_attribute(
        "data-state", "quarantined"
    )
    # The stale tab's download POST is denied while the row is quarantined.
    status, body, _ = post_download(stale, url, encounter, attachment)
    assert status == 403
    assert PDF_BYTES not in body
    # The fresh tab completes the scan; the stale tab's rescan is an
    # idempotent no-op, not a second transition.
    with page.expect_response(lambda r: r.request.method == "POST") as scanned:
        page.locator(f'[data-attachment="{attachment}"] button[value="scan"]').click()
    assert scanned.value.status == 302
    assert stored_state(staff, attachment) == ("available", 1)
    with stale.expect_response(lambda r: r.request.method == "POST") as replay:
        stale.locator(f'[data-attachment="{attachment}"] button[value="scan"]').click()
    assert replay.value.status == 302
    assert stored_state(staff, attachment) == ("available", 1)
    capture(stale, root, "stale-rescan", 1280)
    stale.close()
    return {
        "attachment": attachment,
        "stale_download_status": status,
        "rescan_status": replay.value.status,
        "scan_attempts_after_rescan": 1,
    }


def test_attachment_accessibility_matrix(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """Shared UI matrix: reflow/keyboard, forced colors, reduced motion,
    200% zoom with the native form fallback, and a stale second tab."""
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    seed(staff, MATRIX_DAY)
    browser = renewal_page.context.browser
    assert browser is not None
    report: dict[str, object] = {}
    errors: list[str] = []
    scenes: list[tuple[str, dict[str, Any]]] = [
        ("reflow_320", {"viewport": {"width": 1280, "height": 900}}),
        (
            "forced_colors",
            {"viewport": {"width": 1280, "height": 900}, "forced_colors": "active"},
        ),
        (
            "reduced_motion",
            {"viewport": {"width": 375, "height": 900}, "reduced_motion": "reduce"},
        ),
        (
            "zoom_200_native_fallback",
            {
                "viewport": {
                    "width": ZOOM_WINDOW // ZOOM_FACTOR,
                    "height": 900 // ZOOM_FACTOR,
                },
                "device_scale_factor": ZOOM_FACTOR,
                "java_script_enabled": False,
            },
        ),
        ("stale_tab", {"viewport": {"width": 1280, "height": 900}}),
    ]
    for scene, options in scenes:
        context = new_context(browser, locale="pt-BR", **options)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type == "error" else None
            ),
        )
        try:
            url, encounter = _open_workspace(page, staff, base, MATRIX_DAY)
            if scene == "reflow_320":
                report[scene] = _reflow_scene(page, root)
            elif scene == "forced_colors":
                report[scene] = _forced_colors_scene(page, root)
            elif scene == "reduced_motion":
                report[scene] = _reduced_motion_scene(page)
            elif scene == "zoom_200_native_fallback":
                report[scene] = _zoom_200_scene(page, url, encounter, root)
            else:
                report[scene] = _stale_scene(page, staff, url, encounter, root)
        finally:
            context.close()
    assert not errors
    report["page_errors"] = errors
    report["captured_at"] = datetime.now(tz=UTC).isoformat()
    destination = root / "attachments" / "accessibility-report.json"
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
