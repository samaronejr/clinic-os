"""Real reception arrivals, room calls and independently authenticated displays."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser import test_agenda_slice as slice_fixture
from renewal.browser._fixture_secrets import FixtureSecret
from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
)
from renewal.browser._page_wait import evaluate_js
from renewal.browser.a11y_support import AXE_RUN_JS, AXE_URL
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_agenda import (
    _enrol,
    _sign_in_receptionist,
    agenda_staff,
)
from renewal.browser.test_agenda_grid import _set_preferences
from renewal.browser.test_agenda_slice import SliceWorld

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page, ViewportSize

__all__ = ("agenda_staff",)

NO_OVERFLOW: Final = "document.documentElement.scrollWidth <= innerWidth"
SENTINELS: Final = (
    FixtureSecret("SINTETICO-SENTINELA-PATIENT"),
    FixtureSecret("Consulta sintética"),
    FixtureSecret("Teleconsulta sintética"),
)


@pytest.fixture(scope="module")
def reception_world(agenda_staff: dict[str, str]) -> SliceWorld:
    """Reuse real scheduling seeds on deterministic dates in this owned database."""
    world = SliceWorld(agenda_staff)
    world.next_day = date(2036, 4, 6)
    with world._owner() as connection:
        connection.execute(
            "INSERT INTO clinic_app.scheduling_resource "
            "(id,organization_id,clinic_id,kind,name,capacity,active) "
            "VALUES (%s,%s,%s,'location','Recepcao sintetica',1,true)",
            [str(uuid4()), agenda_staff["organization"], agenda_staff["clinic_a"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.scheduling_resource "
            "(id,organization_id,clinic_id,kind,name,capacity,active) "
            "VALUES (%s,%s,%s,'room','Sala sintetica de apoio',2,true)",
            [str(uuid4()), agenda_staff["organization"], agenda_staff["clinic_a"]],
        )
    return world


@pytest.fixture(scope="module")
def reception_review_day(reception_world: SliceWorld) -> str:
    """Keep a populated, independent day for every responsive capture."""
    return reception_world.seed_day().day


@pytest.fixture
def sentinel_booking(
    reception_world: SliceWorld, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """Seed real encrypted sentinel names in a room reserved for this scenario."""
    enrol = _enrol
    with monkeypatch.context() as fixture_inputs:
        fixture_inputs.setattr(
            slice_fixture,
            "_enrol",
            lambda connection, staff, _name, clinic: enrol(
                connection, staff, SENTINELS[0], clinic
            ),
        )
        seeded = reception_world.seed_day()
    appointment_ids = [
        identifier for rows in seeded.by_physician.values() for identifier in rows
    ]
    with reception_world._owner() as connection:
        target = connection.execute(
            "SELECT a.id FROM clinic_app.scheduling_appointment a "
            "JOIN clinic_app.scheduling_appointmentresource ar "
            "ON ar.appointment_id=a.id WHERE a.id=ANY(%s::uuid[]) "
            "AND ar.resource_id=%s ORDER BY a.start_at,a.id LIMIT 1",
            [appointment_ids, reception_world.rooms[1]],
        ).fetchone()
    assert target is not None
    return seeded.day, str(target[0])


@pytest.fixture
def reception_page(
    renewal_page: Page,
    renewal_base_url: str,
    reception_world: SliceWorld,
) -> Page:
    """Keep browser authority in cookies and close every scene in its owner fixture."""
    _sign_in_receptionist(renewal_page, renewal_base_url, reception_world.staff)
    goto_settled(renewal_page, _url(renewal_base_url, reception_world))
    return renewal_page


def _url(base_url: str, world: SliceWorld) -> str:
    return f"{base_url}/scheduling/clinics/{world.staff['clinic_a']}/reception/"


def _day(page: Page, day: str) -> None:
    page.locator("#reception-day").fill(day)
    page.get_by_role("button", name=gettext("Open day"), exact=True).focus()
    with expect_document(page):
        page.keyboard.press("Enter")


def _arrival(page: Page, appointment: str) -> None:
    selected_day = page.locator("#reception-day").input_value()
    form = page.locator(
        ".reception-arrival form:has("
        f"input[name=appointment_id][value='{appointment}'])"
    )
    button = form.get_by_role("button", name=gettext("Register arrival"), exact=True)
    button.focus()
    with expect_document(page):
        page.keyboard.press("Enter")
    expect(page.locator(".reception-ticket").first).to_be_visible()
    expect(page.locator("#reception-day")).to_have_value(selected_day)


def _issued_token(page: Page) -> FixtureSecret:
    """Read the one-time device token on the page that just issued it.

    The realtime ``ready`` reconciles the new page. With the token shown,
    reception.js reveals the stale notice above the board and moves the token
    summary down. A click whose pointerdown lands before that move and whose
    pointerup lands after it is lost, because Playwright checks only the first
    event's hit target. So await the handshake, then click a settled page.
    """
    expect(page.locator(".reception")).to_have_attribute(
        "data-realtime-state", "connected"
    )
    disclosure = page.get_by_text(gettext("Show device token"), exact=True)
    disclosure.click()
    token = FixtureSecret(
        page.get_by_role(
            "textbox", name=gettext("Device token — shown only once"), exact=True
        ).input_value()
    )
    disclosure.click()
    return token


def _axe(page: Page, base_url: str, output: Path) -> None:
    page.add_script_tag(url=base_url + AXE_URL)
    violations = evaluate_js(page, AXE_RUN_JS)
    output.write_text(json.dumps(violations, indent=2) + "\n")
    assert violations == []


def _paused_views(page: Page, pause: Locator, root: Path) -> None:
    full_page_screenshot(page, root / "paused-display.png")
    for width in (375, 768, 1280):
        page.set_viewport_size({"width": width, "height": 900})
        expect(pause).to_have_attribute("aria-pressed", "true")
        full_page_screenshot(page, root / f"paused-display-{width}.png")
    page.emulate_media(forced_colors="active")
    assert evaluate_js(page, "matchMedia('(forced-colors: active)').matches")
    assert (
        evaluate_js(pause, "button => getComputedStyle(button).textDecorationLine")
        == "underline"
    )
    full_page_screenshot(page, root / "paused-display-forced-colors.png")
    page.emulate_media(forced_colors="none")


def test_arrive_queue_call_to_room_with_native_keyboard_forms(
    reception_page: Page,
    reception_world: SliceWorld,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    seeded = reception_world.seed_day()
    root = renewal_artifact_root / "reception"
    root.mkdir(mode=0o700, exist_ok=True)
    size: ViewportSize = {"width": 1280, "height": 900}
    browser = reception_page.context.browser
    assert browser is not None
    context = browser.new_context(
        storage_state=reception_page.context.storage_state(),
        locale="pt-BR",
        viewport=size,
        record_video_dir=str(root / "video"),
        record_video_size=size,
    )
    page = context.new_page()
    video = page.video
    try:
        goto_settled(page, _url(renewal_base_url, reception_world))
        _day(page, seeded.day)
        _arrival(page, seeded.by_physician[reception_world.physicians[0]][0])
        page.locator(".reception-call select[name=room_id]").first.select_option(
            reception_world.rooms[0]
        )
        call = page.get_by_role(
            "button", name=gettext("Call to room"), exact=True
        ).first
        call.focus()
        with expect_document(page):
            page.keyboard.press("Enter")
        expect(page.locator(".reception-queue-row").first).to_contain_text(
            gettext("Called")
        )
        expect(page.locator(".reception-room").first).to_contain_text(
            gettext("Occupied")
        )
        expect(page.locator("#reception-day")).to_have_value(seeded.day)
        full_page_screenshot(page, root / "called-desktop.png")
        _axe(page, renewal_base_url, root / "axe-called.json")
    finally:
        context.close()
        if video is not None:
            video.save_as(root / "reception.webm")
        (root / "happy-cleanup.json").write_text(
            json.dumps({"browser_context_closed": True}) + "\n"
        )


def test_display_has_no_phi_and_revocation_matches_forged_token(
    reception_page: Page,
    reception_world: SliceWorld,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    sentinel_booking: tuple[str, str],
) -> None:
    root = renewal_artifact_root / "reception"
    root.mkdir(mode=0o700, exist_ok=True)
    _day(reception_page, sentinel_booking[0])
    expect(reception_page.locator(".reception-arrival").first).to_contain_text(
        SENTINELS[0]
    )
    _arrival(reception_page, sentinel_booking[1])
    reception_page.locator(".reception-call select[name=room_id]").last.select_option(
        reception_world.rooms[1]
    )
    click_to_navigate(
        reception_page.get_by_role(
            "button", name=gettext("Call to room"), exact=True
        ).last
    )
    expect(reception_page.locator(".reception-queue-row").last).to_contain_text(
        gettext("Called")
    )
    reception_page.locator("details").filter(
        has=reception_page.get_by_text(gettext("Waiting display devices"), exact=True)
    ).locator("summary").click()
    click_to_navigate(
        reception_page.get_by_role(
            "button", name=gettext("Issue display token"), exact=True
        )
    )
    token = _issued_token(reception_page)
    browser = reception_page.context.browser
    assert browser is not None
    context = browser.new_context(locale="pt-BR")
    page = context.new_page()
    try:
        response = goto_settled(page, renewal_base_url + "/waiting-display/")
        assert response is not None
        absent = response.body()
        forged_response = context.request.get(
            renewal_base_url + "/waiting-display/",
            headers={"Authorization": "Bearer " + "x" * 43},
        )
        assert (response.status, forged_response.status) == (403, 403)
        assert forged_response.body() == absent
        page.locator("#display-token").fill(token)
        click_to_navigate(
            page.get_by_role("button", name=gettext("Connect display"), exact=True)
        )
        expect(page.get_by_role("heading", name=gettext("Now calling"))).to_be_visible()
        assert all(sentinel not in page.content() for sentinel in (*SENTINELS, token))
        full_page_screenshot(page, root / "public-display.png")
        _axe(page, renewal_base_url, root / "axe-display.json")
        pause = page.get_by_role("button", name=gettext("Pause automatic updates"))
        pause.click()
        expect(pause).to_have_attribute("aria-pressed", "true")
        _paused_views(page, pause, root)
        goto_settled(reception_page, _url(renewal_base_url, reception_world))
        reception_page.locator("details").filter(
            has=reception_page.get_by_text(
                gettext("Waiting display devices"), exact=True
            )
        ).locator("summary").click()
        evaluate_js(
            reception_page,
            "document.body.dispatchEvent(new CustomEvent('rt:queue'))",
        )
        expect(reception_page.locator("[data-reception-stale]")).to_be_visible()
        expect(reception_page.locator(".reception-section[open]")).to_have_attribute(
            "open", ""
        )
        click_to_navigate(
            reception_page.get_by_role(
                "button", name=gettext("Revoke device"), exact=True
            ).last
        )
        with page.expect_response(
            lambda result: result.url == renewal_base_url + "/waiting-display/"
        ) as revoked:
            pause.click()
        assert revoked.value.status == 403
        expect(
            page.get_by_role("heading", name=gettext("Device disconnected"))
        ).to_be_visible()
        assert revoked.value.body() == absent
        full_page_screenshot(page, root / "revoked-display.png")
        (root / "display-privacy.json").write_text(
            json.dumps(
                {
                    "sentinel_scan": "passed",
                    "forged_status": 403,
                    "revoked_status": 403,
                    "staff_cookie_required": False,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()


def test_reception_refetches_after_another_desk_registers_arrival(
    reception_page: Page,
    reception_world: SliceWorld,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    seeded = reception_world.seed_day()
    expect(reception_page.locator(".reception")).to_have_attribute(
        "data-realtime-state", "connected"
    )
    before = reception_page.locator(".reception-ticket").count()
    browser = reception_page.context.browser
    assert browser is not None
    context = browser.new_context(
        storage_state=reception_page.context.storage_state(), locale="pt-BR"
    )
    second = context.new_page()
    try:
        url = _url(renewal_base_url, reception_world)
        goto_settled(second, url)
        _day(second, seeded.day)
        with reception_page.expect_response(
            lambda response: (
                response.url == url
                and response.request.method == "POST"
                and response.request.headers.get("hx-request") == "true"
            ),
            timeout=2000,
        ):
            _arrival(second, seeded.by_physician[reception_world.physicians[0]][0])
        expect(reception_page.locator(".reception-ticket")).to_have_count(
            before + 1, timeout=2000
        )
        edited = reception_page.locator("#reception-day")
        edited.fill("2040-01-02")
        edited.focus()
        reception_page.get_by_role(
            "heading", name=gettext("Reception Today"), exact=True
        ).click()
        expect(edited).not_to_be_focused()
        _day(second, seeded.day)
        _arrival(second, seeded.by_physician[reception_world.physicians[0]][1])
        expect(reception_page.locator("[data-reception-stale]")).to_be_visible(
            timeout=2000
        )
        expect(edited).to_have_value("2040-01-02")
        root = renewal_artifact_root / "reception"
        root.mkdir(mode=0o700, exist_ok=True)
        (root / "realtime.json").write_text(
            json.dumps(
                {
                    "trigger": "another desk registered arrival",
                    "observed": (
                        "queue hint refetched the board; a second hint "
                        "preserved an edited form and showed the stale notice"
                    ),
                    "timeout_ms": 2000,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()


@pytest.mark.parametrize(
    "configuration",
    [
        (width, theme, density)
        for width in (1280, 640, 375, 320)
        for theme, density in (("light", "comfortable"), ("dark", "compact"))
    ],
)
def test_reception_reflows_and_is_accessible(
    reception_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    configuration: tuple[int, str, str],
    reception_review_day: str,
) -> None:
    width, theme, density = configuration
    reception_page.emulate_media(reduced_motion="reduce")
    root = renewal_artifact_root / "reception"
    root.mkdir(mode=0o700, exist_ok=True)
    reception_url = reception_page.url
    _set_preferences(reception_page, renewal_base_url, theme, density)
    goto_settled(reception_page, reception_url)
    _day(reception_page, reception_review_day)
    reception_page.set_viewport_size({"width": width, "height": 900})
    assert evaluate_js(reception_page, NO_OVERFLOW)
    assert evaluate_js(
        reception_page,
        "matchMedia('(prefers-reduced-motion: reduce)').matches",
    )
    name = f"today-{width}-{theme}-{density}"
    full_page_screenshot(reception_page, root / f"{name}.png")
    reception_page.screenshot(path=str(root / f"{name}-top.png"))
    evaluate_js(
        reception_page, "document.getElementById('queue-title').scrollIntoView()"
    )
    expect(reception_page.locator("#queue-title")).to_be_in_viewport()
    reception_page.screenshot(path=str(root / f"{name}-queue.png"))
    evaluate_js(
        reception_page, "document.getElementById('rooms-title').scrollIntoView()"
    )
    expect(reception_page.locator("#rooms-title")).to_be_in_viewport()
    reception_page.screenshot(path=str(root / f"{name}-rooms.png"))
    reception_page.get_by_role("contentinfo").scroll_into_view_if_needed()
    reception_page.screenshot(path=str(root / f"{name}-end.png"))
    _axe(reception_page, renewal_base_url, root / f"axe-{name}.json")


def test_native_forms_work_without_javascript(
    reception_page: Page,
    reception_world: SliceWorld,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    browser = reception_page.context.browser
    assert browser is not None
    context = browser.new_context(
        storage_state=reception_page.context.storage_state(),
        locale="pt-BR",
        java_script_enabled=False,
    )
    page = context.new_page()
    try:
        goto_settled(page, _url(renewal_base_url, reception_world))
        room = page.locator(".reception-room").filter(
            has=page.get_by_role(
                "heading", name="Sala sintetica de apoio · 1", exact=True
            )
        )
        room.locator("select[name=state]").select_option("cleaning")
        click_to_navigate(
            room.get_by_role("button", name=gettext("Update room"), exact=True)
        )
        expect(room.locator("select[name=state]")).to_have_value("cleaning")
        room.locator("select[name=state]").select_option("available")
        click_to_navigate(
            room.get_by_role("button", name=gettext("Update room"), exact=True)
        )
        expect(room.locator("select[name=state]")).to_have_value("available")
        root = renewal_artifact_root / "reception"
        root.mkdir(mode=0o700, exist_ok=True)
        full_page_screenshot(page, root / "native-no-javascript.png")
    finally:
        context.close()


def test_offline_actions_never_claim_success(
    reception_page: Page,
    reception_world: SliceWorld,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    context = reception_page.context
    try:
        context.set_offline(True)
        expect(reception_page.locator("[data-reception-offline]")).to_be_visible()
        reception_page.get_by_role(
            "button", name=gettext("Open day"), exact=True
        ).click()
        expect(reception_page.locator("[data-reception-saving]")).to_be_hidden()
        root = renewal_artifact_root / "reception"
        root.mkdir(mode=0o700, exist_ok=True)
        full_page_screenshot(reception_page, root / "offline.png")
    finally:
        context.set_offline(False)
    goto_settled(reception_page, _url(renewal_base_url, reception_world))
    expect(reception_page.locator("[data-reception-offline]")).to_be_hidden()
