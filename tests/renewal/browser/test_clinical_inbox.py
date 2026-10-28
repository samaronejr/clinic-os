"""Real-browser Clinical Inbox and clinician Today journeys (plan item 32).

Results enter through the real orders workspace, then the inbox acknowledges,
bulk-previews, refreshes and refuses on the runtime role with real middleware,
CSRF, PostgreSQL policies and vendored axe. Hints are dispatched as the same
DOM events the realtime client raises; no timer drives any assertion.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django.utils.translation import gettext as _
from ops.testing.process_helpers import run_process
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import worker_dsn
from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
)
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import element_box, full_page_screenshot
from renewal.browser.results_support import place, seed_result_patient, upload
from renewal.browser.support_managers import seed_ai_admin
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_realtime import _today_scene

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Locator, Page

__all__ = ("availability_staff",)
DAYS = {1280: "2039-07-12", 768: "2039-07-13", 375: "2039-07-14"}
SUITE = "clinical-inbox"
EMPTY_KEYS = {
    "items",
    "counts",
    "total",
    "overdue",
    "critical",
    "removed",
    "truncated",
    "counts_complete",
}


def _directory(root: Path) -> Path:
    directory = root / SUITE
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def capture(page: Page, root: Path, state: str, width: int) -> None:
    """Retain the rendered surface and reject horizontal page overflow."""
    page.emulate_media(reduced_motion="reduce")
    full_page_screenshot(page, _directory(root) / f"{state}-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def themed(page: Page, root: Path, state: str, width: int) -> None:
    for theme in ("light", "dark"):
        page.locator("html").evaluate(
            "(node, theme) => { node.dataset.theme = theme; }", theme
        )
        capture(page, root, f"{state}-{theme}", width)
    page.locator("html").evaluate("node => { node.dataset.theme = 'light'; }")


def targets(page: Page) -> None:
    for control in page.locator("main button:visible, main a.button:visible").all():
        bounds = element_box(control)
        assert bounds["height"] >= 44, bounds
        assert bounds["width"] >= 44, bounds


def context_for(
    browser: Browser,
    seed: Page,
    *,
    width: int,
    video: Path | None = None,
    javascript: bool = True,
) -> BrowserContext:
    return browser.new_context(
        storage_state=seed.context.storage_state(),
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": width, "height": 900},
        java_script_enabled=javascript,
        record_video_dir=str(video) if video is not None else None,
    )


def inbox_url(base_url: str, staff: dict[str, str]) -> str:
    return f"{base_url}/clinics/{staff['clinic_a']}/inbox/"


def source_keys(staff: dict[str, str], data: dict[str, str]) -> list[str]:
    """Owner-side oracle: this patient's source versions, never another test's."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        rows = conn.execute(
            "SELECT v.id FROM clinic_app.orders_resultversion v "
            "JOIN clinic_app.orders_order o ON o.id=v.proposed_order_id "
            "WHERE o.enrollment_id=%s ORDER BY v.created_at",
            [data["enrollment"]],
        ).fetchall()
    assert rows
    return [f"result:{row[0]}" for row in rows]


def results_of(page: Page, keys: list[str]) -> Locator:
    return page.locator(", ".join(f'li[data-inbox-key="{key}"]' for key in keys))


def post(page: Page, url: str, control: Locator) -> int:
    """Observe the real POST for an enhanced or native inbox command."""
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as posted:
        control.click()
    return posted.value.status


def upload_results(
    page: Page, base_url: str, staff: dict[str, str], data: dict[str, str], *flags: str
) -> None:
    goto_settled(page, f"{base_url}/clinics/{staff['clinic_a']}/results/")
    place(page, data)
    for flag in flags:
        upload(page, data, flag=flag)


def revoke_care_team(staff: dict[str, str], data: dict[str, str]) -> None:
    """Legal lifecycle transition: revoke the physician's patient scope."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        updated = conn.execute(
            "UPDATE clinic_app.identity_careteammembership SET revoked_at=now() "
            "WHERE user_id=%s AND patient_enrollment_id=%s AND revoked_at IS NULL",
            [staff["physician_a_id"], data["enrollment"]],
        ).rowcount
    assert updated == 1


def decisions(staff: dict[str, str], data: dict[str, str]) -> tuple[list[str], int]:
    """Owner-side oracle: dispositions and releases for this patient's sources."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        dispositions = [
            str(row[0])
            for row in conn.execute(
                "SELECT a.disposition FROM clinic_app.orders_resultacknowledgment a "
                "JOIN clinic_app.orders_resultmatch m ON m.version_id=a.version_id "
                "JOIN clinic_app.orders_order o ON o.id=m.order_id "
                "WHERE o.enrollment_id=%s ORDER BY a.disposition",
                [data["enrollment"]],
            ).fetchall()
        ]
        released = conn.execute(
            "SELECT count(*) FROM clinic_app.orders_resultrelease r "
            "JOIN clinic_app.orders_resultmatch m ON m.version_id=r.version_id "
            "JOIN clinic_app.orders_order o ON o.id=m.order_id "
            "WHERE o.enrollment_id=%s",
            [data["enrollment"]],
        ).fetchone()
    assert released is not None
    return dispositions, int(released[0])


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_inbox_acknowledges_a_result_from_the_queue(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, root = availability_staff, renewal_artifact_root
    data = seed_result_patient(staff, DAYS[width])
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    browser = renewal_page.context.browser
    assert browser is not None
    video = _directory(root) / "video" if width == 1280 else None
    context = context_for(browser, renewal_page, width=width, video=video)
    page = context.new_page()
    url = inbox_url(renewal_base_url, staff)
    try:
        upload_results(page, renewal_base_url, staff, data, "normal", "high")
        keys = source_keys(staff, data)
        response = goto_settled(page, url)
        assert response is not None
        assert response.status == 200
        expect(page.get_by_role("heading", level=1)).to_have_text(_("Clinical Inbox"))
        items = results_of(page, keys)
        expect(items).to_have_count(2)
        normal = items.filter(has=page.locator("input[data-worklist-select]"))
        high = items.filter(has_not=page.locator("input[data-worklist-select]"))
        expect(normal).to_have_count(1)
        expect(high).to_have_count(1)
        expect(normal.locator(".badge--success")).to_have_text(_("All values normal"))
        assert page.url == url
        themed(page, root, "queue", width)
        check_page(page, renewal_base_url, root, f"inbox-queue-{width}")
        targets(page)
        high.locator('[name="disposition"]').select_option("follow_up")
        status = post(page, url, high.get_by_role("button", name=_("Acknowledge")))
        assert status == 200
        expect(page.locator("[data-inbox-outcome]")).to_be_focused()
        expect(results_of(page, keys)).to_have_count(1)
        themed(page, root, "acknowledged", width)
        check_page(page, renewal_base_url, root, f"inbox-acknowledged-{width}")
        assert decisions(staff, data) == (["follow_up"], 0)
        page.emulate_media(forced_colors="active", reduced_motion="reduce")
        for reflow_width in (640, 320):
            page.set_viewport_size({"width": reflow_width, "height": 900})
            capture(page, root, f"reflow-from-{width}", reflow_width)
    finally:
        context.close()
        if page.video is not None:
            movie = _directory(root) / "inbox.webm"
            page.video.save_as(movie)
            movie.chmod(0o600)


def test_bulk_acknowledgment_previews_each_normal_result_and_never_releases(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, root = availability_staff, renewal_artifact_root
    data = seed_result_patient(staff, "2039-07-15")
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    page = renewal_page
    page.set_viewport_size({"width": 1280, "height": 900})
    url = inbox_url(renewal_base_url, staff)
    upload_results(page, renewal_base_url, staff, data, "normal", "normal", "critical")
    keys = source_keys(staff, data)
    goto_settled(page, url)
    items = results_of(page, keys)
    expect(items).to_have_count(3)
    # Then: the critical source leads and is never offered for bulk work.
    expect(items.first).to_have_class("worklist-item worklist-item--critical")
    expect(items.first.locator("input[data-worklist-select]")).to_have_count(0)
    board = page.locator("#inbox-board")
    expect(board.get_by_role("button", name=_("Release"))).to_have_count(0)
    expect(board.locator('input[name="action"][value*="release"]')).to_have_count(0)
    normal = items.filter(has=page.locator("input[data-worklist-select]"))
    expect(normal).to_have_count(2)
    for index in range(2):
        # When: the clinician triages with the keyboard and selects with X.
        normal.nth(index).focus()
        page.keyboard.press("x")
        expect(normal.nth(index).locator("input[data-worklist-select]")).to_be_checked()
    status = post(
        page, url, page.get_by_role("button", name=_("Preview selected normal results"))
    )
    assert status == 200
    preview = page.locator("[data-bulk-preview]")
    expect(preview).to_have_attribute("data-bulk-preview", "2")
    expect(preview.locator(".worklist-preview-item")).to_have_count(2)
    for row in preview.locator(".worklist-preview-item").all():
        expect(row.get_by_role("table")).to_contain_text("7,10")
    capture(page, root, "bulk-preview", 1280)
    check_page(page, renewal_base_url, root, "inbox-bulk-preview")
    assert decisions(staff, data) == ([], 0)
    confirm = preview.get_by_role("button", name=_("Acknowledge"))
    assert post(page, url, confirm) == 200
    expect(page.locator("[data-inbox-outcome]")).to_be_focused()
    expect(results_of(page, keys)).to_have_count(1)
    expect(results_of(page, keys).first).to_have_class(
        "worklist-item worklist-item--critical"
    )
    capture(page, root, "bulk-acknowledged", 1280)
    assert decisions(staff, data) == (["no_action", "no_action"], 0)


def test_revoked_item_is_removed_with_a_notice_on_refresh(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, root = availability_staff, renewal_artifact_root
    data = seed_result_patient(staff, "2039-07-16")
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    page = renewal_page
    page.set_viewport_size({"width": 1280, "height": 900})
    url = inbox_url(renewal_base_url, staff)
    upload_results(page, renewal_base_url, staff, data, "normal")
    keys = source_keys(staff, data)
    goto_settled(page, url)
    expect(results_of(page, keys)).to_have_count(1)
    expect(page.locator("[data-inbox-removed]")).to_have_count(0)
    # When: the patient scope is revoked while the item is open on screen.
    revoke_care_team(staff, data)
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as refreshed:
        page.evaluate("() => document.body.dispatchEvent(new CustomEvent('rt:inbox'))")
    assert refreshed.value.status == 200
    notice = page.locator("[data-inbox-removed]")
    expect(notice).to_have_attribute("data-inbox-removed", "1")
    expect(notice).to_have_text(
        _("One item was removed or is no longer available to you.")
    )
    expect(results_of(page, keys)).to_have_count(0)
    capture(page, root, "revoked-removed", 1280)
    check_page(page, renewal_base_url, root, "inbox-revoked")


def test_inbox_native_without_javascript(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, root = availability_staff, renewal_artifact_root
    data = seed_result_patient(staff, "2039-07-17")
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    browser = renewal_page.context.browser
    assert browser is not None
    url = inbox_url(renewal_base_url, staff)
    with context_for(browser, renewal_page, width=375, javascript=False) as context:
        page = context.new_page()
        upload_results(page, renewal_base_url, staff, data, "normal", "low")
        keys = source_keys(staff, data)
        goto_settled(page, url)
        low = results_of(page, keys).filter(
            has_not=page.locator("input[data-worklist-select]")
        )
        low.locator('[name="disposition"]').select_option("urgent_review")
        assert post(page, url, low.get_by_role("button", name=_("Acknowledge"))) == 200
        expect(page.locator("[data-inbox-outcome]")).to_be_visible()
        expect(results_of(page, keys)).to_have_count(1)
        revoke_care_team(staff, data)
        refresh = page.locator("form[data-worklist-refresh] button")
        assert post(page, url, refresh) == 200
        expect(page.locator("[data-inbox-removed]")).to_have_attribute(
            "data-inbox-removed", "1"
        )
        expect(results_of(page, keys)).to_have_count(0)
        capture(page, root, "native-no-javascript", 375)
    assert decisions(staff, data) == (["urgent_review"], 0)


def _arrived_today(staff: dict[str, str], patient: str) -> str:
    """Seed an arrived visit at the next database minute; return its id.

    The scheduled insert meets the real future-start guard, and
    ``_today_scene`` keeps that minute hours before clinic-local midnight.
    The booked physician records the arrival, as the shared seed does.
    """
    appointment = str(uuid4())
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        window = conn.execute(
            "SELECT date_trunc('minute', now()) + interval '1 minute', "
            "date_trunc('minute', now()) + interval '16 minutes'"
        ).fetchone()
        assert window is not None
        scope = [staff["organization"], staff["clinic_a"], staff["physician_a_id"]]
        conn.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock "
            "(id,organization_id,clinic_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
            [str(uuid4()), *scope, *window, str(uuid4()), secrets.token_bytes(32)],
        )
        conn.execute(
            "INSERT INTO clinic_app.scheduling_appointment "
            "(id,organization_id,clinic_id,patient_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'scheduled',now(),now())",
            [
                appointment,
                staff["organization"],
                staff["clinic_a"],
                patient,
                staff["physician_a_id"],
                *window,
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [staff["physician_a_id"]],
        )
        conn.execute(
            "UPDATE clinic_app.scheduling_appointment SET status='arrived' WHERE id=%s",
            [appointment],
        )
    return appointment


def test_today_shows_next_patient_brief_and_inbox_counts(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    # An isolated clinic whose local day has at least 12 hours left.
    staff, root = _today_scene(availability_staff), renewal_artifact_root
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        tomorrow = conn.execute(
            "SELECT ((now() AT TIME ZONE timezone)::date + 1)::text "
            "FROM clinic_app.identity_clinic WHERE id=%s",
            [staff["clinic_a"]],
        ).fetchone()
    assert tomorrow is not None
    # The shared seed's own visit is tomorrow, a valid future start; today's
    # arrived visit for the same patient starts at the next database minute.
    data = seed_result_patient(staff, str(tomorrow[0]))
    data["appointment"] = _arrived_today(staff, data["patient"])
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    page = renewal_page
    upload_results(page, renewal_base_url, staff, data, "normal")
    url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/today/"
    for width in (1280, 768, 375):
        page.set_viewport_size({"width": width, "height": 900})
        response = goto_settled(page, url)
        assert response is not None
        assert response.status == 200
        expect(page.get_by_role("heading", level=1)).to_have_text(_("Today"))
        expect(page.locator("[data-today-next]")).to_have_attribute(
            "data-today-next", "arrived"
        )
        expect(page.locator(".today-next-name")).to_have_text(data["source_name"])
        brief = page.locator("[data-today-brief]")
        expect(brief).to_have_attribute("data-today-brief", "unavailable")
        expect(brief).to_contain_text(_("Brief unavailable"))
        counts = page.locator('[data-inbox-count="result"] dd')
        assert int(counts.inner_text()) >= 1
        themed(page, root, "today", width)
        check_page(page, renewal_base_url, root, f"today-{width}")
        targets(page)
    # When: J moves to the first appointment and Enter starts its encounter.
    page.keyboard.press("j")
    row = page.locator("li.today-appointment").first
    expect(row).to_be_focused()
    expect(row).to_have_attribute("aria-current", "true")
    with expect_document(page):
        page.keyboard.press("Enter")
    assert "/ehr/clinics/" in page.url
    assert data["appointment"] not in page.url
    goto_settled(page, url)
    expect(
        page.locator(".today-next").get_by_role("button", name=_("Continue encounter"))
    ).to_be_enabled()


def _brief_command(
    patch: pytest.MonkeyPatch, dsn: str, *arguments: str
) -> dict[str, str]:
    with patch.context() as local:
        for name, value in {
            "APP_DATABASE_URL": dsn,
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
            "CELERY_BROKER_URL": "memory://",
            "PYTHONPATH": os.pathsep.join((".", "tests")),
        }.items():
            local.setenv(name, value)
        # Playwright's synchronous driver owns this thread's running loop.
        # The bounded async subprocess helper needs its own thread and loop.
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(
                run_process,
                (
                    sys.executable,
                    "-c",
                    "import django; django.setup(); "
                    "from renewal.chart_brief_fixture import main; main()",
                    *arguments,
                ),
                timeout_seconds=60,
            ).result()
    assert result.returncode == 0, result.stderr
    data: dict[str, str] = json.loads(result.stdout)
    return data


def _brief_post(page: Page, button: Locator) -> None:
    with page.expect_response(
        lambda response: (
            response.url.endswith("/chart-assistant/")
            and response.request.method == "POST"
        )
    ) as posted:
        button.click()
    assert posted.value.status == 200


def chart_brief_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the worker-backed Today journey from the agenda worker suite."""
    staff = _today_scene(availability_staff)
    with psycopg.connect(staff["dsn"]) as owner:
        owner.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        day = owner.execute("SELECT (current_date + 1)::text").fetchone()
    assert day is not None
    data = seed_result_patient(staff, str(day[0]))
    data["appointment"] = _arrived_today(staff, data["patient"])
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    page = renewal_page
    url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/today/"
    goto_settled(page, url)
    with expect_document(page):
        page.locator(".today-next button[type=submit]").click()
    expect(page.locator("[data-encounter]")).to_have_count(1)
    goto_settled(page, url)
    administrator = seed_ai_admin(staff, "light")
    assert (
        _brief_command(
            monkeypatch,
            staff["dsn"],
            "seed",
            administrator["id"],
            staff["clinic_a"],
            staff["organization"],
        )["status"]
        == "seeded"
    )
    goto_settled(page, url)
    _brief_post(page, page.get_by_role("button", name=_("Generate pre-visit brief")))
    expect(page.locator("#chart-panel")).to_have_attribute(
        "data-chart-state", "generating"
    )
    with psycopg.connect(staff["dsn"]) as owner:
        owner.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        operation = owner.execute(
            "SELECT i.operation_id FROM clinic_app.ai_aiinvocation i "
            "JOIN clinic_app.ai_aiartifact a ON a.id=i.inputs_ref_id "
            "WHERE a.patient_enrollment_id=%s ORDER BY i.created_at DESC LIMIT 1",
            [data["enrollment"]],
        ).fetchone()
    assert operation is not None
    assert (
        _brief_command(monkeypatch, worker_dsn(), "execute", str(operation[0]))[
            "status"
        ]
        == "succeeded"
    )
    _brief_post(page, page.get_by_role("button", name=_("Refresh brief")))
    expect(page.locator("[data-today-brief]")).to_have_attribute(
        "data-today-brief", "ready"
    )
    expect(page.locator('[data-chart-citation="encounter"]')).to_have_count(1)
    for width in (1280, 375, 320):
        page.set_viewport_size({"width": width, "height": 900})
        themed(page, renewal_artifact_root, "brief", width)
        check_page(page, renewal_base_url, renewal_artifact_root, f"brief-{width}")
        targets(page)
    page.locator("#chart-question").fill("What allergies does Synthetic Other have?")
    _brief_post(page, page.get_by_role("button", name=_("Ask the chart")))
    expect(page.locator('[data-chart-answer="refused"]')).to_be_visible()
    assert "Synthetic Other" not in page.locator("#chart-panel").inner_text()
    themed(page, renewal_artifact_root, "brief-refused", 320)
    assert _brief_command(monkeypatch, staff["dsn"], "down")["status"] == "unavailable"
    _brief_post(page, page.get_by_role("button", name=_("Generate pre-visit brief")))
    expect(page.locator("#chart-panel")).to_have_attribute(
        "data-chart-state", "unavailable"
    )
    expect(page.locator("#chart-panel")).to_contain_text(_("Brief unavailable"))
    themed(page, renewal_artifact_root, "brief-unavailable", 320)
    with expect_document(page):
        page.locator(".today-next button[type=submit]").click()
    expect(page.locator("[data-encounter]")).to_have_count(1)


def test_receptionist_gets_the_empty_inbox_shape_and_refused_pages(
    renewal_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    data = seed_result_patient(staff, "2039-07-18")
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    upload_results(renewal_page, renewal_base_url, staff, data, "normal")
    api = f"{renewal_base_url}/api/ui/v1/inbox/query/"
    known = [f"result:{uuid4()}"]

    def query(page: Page) -> dict[str, object]:
        csrf = next(
            cookie.get("value", "")
            for cookie in page.context.cookies()
            if cookie.get("name") == "csrftoken"
        )
        response = page.request.post(
            api,
            data=json.dumps({"clinic_id": staff["clinic_a"], "known": known}),
            headers={"Content-Type": "application/json", "X-CSRFToken": csrf},
        )
        assert response.status == 200
        body = response.json()
        assert isinstance(body, dict)
        return body

    working = query(renewal_page)
    assert set(working) == EMPTY_KEYS
    assert working["items"]
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(locale="pt-BR") as context:
        page = context.new_page()
        _sign_in_receptionist(page, renewal_base_url, staff)
        counts = working["counts"]
        assert isinstance(counts, dict)
        assert query(page) == {
            "items": [],
            "counts": dict.fromkeys(counts, 0),
            "total": 0,
            "overdue": 0,
            "critical": 0,
            "removed": known,
            "truncated": False,
            "counts_complete": True,
        }
        for path in ("inbox", "today"):
            refused = goto_settled(
                page, f"{renewal_base_url}/clinics/{staff['clinic_a']}/{path}/"
            )
            assert refused is not None
            assert refused.status == 403
        nav = page.locator('a[data-module="inbox"]')
        if nav.count():
            click_to_navigate(nav)
            assert "/inbox/" not in page.url
