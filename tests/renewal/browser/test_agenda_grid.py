"""Todo 24 agenda board: every view, keyboard moves, races and live updates.

The board extends the todo 23 slice grid (variant A, ADR-001): day by
professional, room and equipment; one lane's week; month counts that drill
into the day; and the list. This suite runs every view at 1280/375/320 in the
light comfortable and dark compact preferences (axe zero, 44px targets, no
page overflow), records a keyboard-only move, a target taken meanwhile (409,
original kept) and two receptionists booking the last free room (one hold,
"Horário acabou de ser ocupado" for the other), shows another unit's busy time
without patient data and the remote-change banner after a realtime refetch,
and proves the JS-off list. Every wait subscribes to a response, a DOM state
or a realtime event, never a timer.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import psycopg
import pytest
from apps.intake.patient_name_index import PURPOSE, normalized_name_bytes
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
    wait_for_signed_in,
)
from renewal.browser._page_wait import evaluate_js, wait_for_js
from renewal.browser._protected import encrypt, kek
from renewal.browser.a11y_support import AXE_RUN_JS, AXE_URL
from renewal.browser.engines import full_page_screenshot, new_context, selected_engine
from renewal.browser.test_agenda import (
    SETTLED_JS,
    _sign_in,
    _sign_in_receptionist,
    _utc,
    agenda_staff,
)
from renewal.browser.test_agenda_slice import (
    CSP_ONLY_JS,
    FOCUSED_APPOINTMENT_JS,
    REMOTE_MOVE_JS,
    RUN_SHIFT,
    TARGETS_JS,
    SliceDay,
    SliceWorld,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Locator,
        Page,
        Response,
        StorageState,
        ViewportSize,
    )

__all__ = ("agenda_staff",)

WIDTHS: Final = (1280, 375, 320)
VIEWS: Final = ("day", "week", "month", "list")
PREFERENCES: Final = (("light", "comfortable"), ("dark", "compact"))
OK: Final = 200
CONFLICT: Final = 409
SLOT_TEXT: Final = "That window is no longer free for this physician or patient."
# The seeded rival shares the mover's room, and the move service reports the
# shared-resource conflict first.
RESOURCE_TEXT: Final = "A required resource is unavailable for this window."
TAKEN_TEXT: Final = "This time was just taken. Choose another time."
NO_OVERFLOW_JS: Final = "document.documentElement.scrollWidth <= window.innerWidth"
REFLOW_WINDOW: Final = (1280, 900)
REFLOW_SCALES: Final = (2, 4)
BOARD_CONTENT: Final = (
    ".agenda-grid .resource-slot,"
    " .agenda-month-link:has(.agenda-month-count),"
    " .agenda-board-list .agenda-row"
)
VIEWPORT_JS: Final = """() => ({
  css_width: innerWidth, css_height: innerHeight,
  device_pixel_ratio: devicePixelRatio,
  visual_viewport_scale: visualViewport.scale,
  root_font_size: getComputedStyle(document.documentElement).fontSize,
  reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
})"""
MOTION_JS: Final = """() => [...document.querySelectorAll('#agenda-shell *')]
  .filter(node => node.getClientRects().length)
  .flatMap(node => ['', '::before', '::after'].flatMap(pseudo => {
    const style = getComputedStyle(node, pseudo || null);
    return [style.transitionDuration, style.animationDuration]
      .some(value => value.split(',').some(duration => parseFloat(duration) !== 0))
      ? [{tag: node.tagName, pseudo}] : [];
  }))"""
ACTION_LINES_JS: Final = """() => Math.max(0,
  ...[...document.querySelectorAll('.agenda-board-list .agenda-actions button')]
    .filter(node => node.getClientRects().length)
    .map(node => {
      const style = getComputedStyle(node);
      const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      const borders = parseFloat(style.borderTopWidth)
        + parseFloat(style.borderBottomWidth);
      return (node.getBoundingClientRect().height - padding - borders)
        / parseFloat(style.lineHeight);
    }))"""


def _letters(size: int = 4) -> str:
    """A letters-only suffix: patient names reject digits."""
    return "".join(chr(ord("a") + int(char, 16)) for char in uuid4().hex[:size])


class GridWorld(SliceWorld):
    """The slice's rooms, services and 40-appointment days, on board-only dates."""

    def __init__(self, staff: dict[str, str]) -> None:
        super().__init__(staff)
        # The slice suite seeds the same physicians from 2033; never share a day.
        self.next_day = date(2034, 9, 4) + RUN_SHIFT

    def patient(self, name: str) -> str:
        """Insert an indexed synthetic patient the palette finds by exact name."""
        enrollment = str(uuid4())
        patient = str(uuid4())
        with self._owner() as connection:
            connection.execute(
                "INSERT INTO clinic_app.intake_patient "
                "(id,organization_id,full_name,birth_date,full_name_index,"
                "created_at) VALUES (%s,%s,%s,%s,(SELECT blind_index FROM "
                "clinic_app.protected_blind_index(%s,%s,%s) "
                "ORDER BY key_version DESC LIMIT 1),now())",
                [
                    patient,
                    self.staff["organization"],
                    encrypt(connection, "intake.patient.full_name", name.encode()),
                    encrypt(connection, "intake.patient.birth_date", b"1991-07-09"),
                    kek(),
                    PURPOSE,
                    normalized_name_bytes(name),
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.intake_patientclinicenrollment "
                "(id,organization_id,clinic_id,patient_id,idempotency_key,"
                "create_fingerprint,created_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
                [
                    enrollment,
                    self.staff["organization"],
                    self.staff["clinic_a"],
                    patient,
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
        return enrollment

    def second_receptionist(self) -> str:
        """A second reception desk login in clinic A (same synthetic password)."""
        username = f"recepcao-b-{uuid4().hex[:4]}"
        user_id = str(uuid4())
        with self._owner() as connection:
            connection.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [
                    user_id,
                    username,
                    make_password(self.staff["password"]),
                    f"{username}@agenda.invalid",
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role, "
                "workflow_bundle_version) VALUES (%s, %s, %s, %s, 'receptionist', 1)",
                [
                    str(uuid4()),
                    user_id,
                    self.staff["organization"],
                    self.staff["clinic_a"],
                ],
            )
        return username

    def other_unit_booking(self) -> tuple[str, str]:
        """Book physician A in clinic B at 14:00 on a fresh day.

        A practitioner's active availability cannot overlap across clinics, so
        that day physician A works clinic A 07:00-14:00 and 15:00-20:00 and
        clinic B 14:00-15:00. Return the day and that patient's name.
        """
        day = self.next_day.isoformat()
        self.next_day += timedelta(days=1)
        name = f"Sintético Outra Unidade {uuid4().hex[:4]}"
        physician = self.staff["physician_a_id"]
        with self._owner() as connection:
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role, "
                "workflow_bundle_version) VALUES (%s, %s, %s, %s, 'physician', 1) "
                "ON CONFLICT DO NOTHING",
                [
                    str(uuid4()),
                    physician,
                    self.staff["organization"],
                    self.staff["clinic_b"],
                ],
            )
            patient = str(uuid4())
            connection.execute(
                "INSERT INTO clinic_app.intake_patient "
                "(id, organization_id, full_name, birth_date, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                [
                    patient,
                    self.staff["organization"],
                    encrypt(connection, "intake.patient.full_name", name.encode()),
                    encrypt(connection, "intake.patient.birth_date", b"1990-01-01"),
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.intake_patientclinicenrollment "
                "(id,organization_id,clinic_id,patient_id,idempotency_key,"
                "create_fingerprint,created_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
                [
                    str(uuid4()),
                    self.staff["organization"],
                    self.staff["clinic_b"],
                    patient,
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
            for clinic, start, end in (
                (self.staff["clinic_a"], "07:00", "14:00"),
                (self.staff["clinic_b"], "14:00", "15:00"),
                (self.staff["clinic_a"], "15:00", "20:00"),
            ):
                connection.execute(
                    "INSERT INTO clinic_app.scheduling_availabilityblock (id,"
                    " organization_id, clinic_id, practitioner_id, resource_id,"
                    " start_at, end_at, idempotency_key, create_fingerprint,"
                    " created_at, updated_at, retired_at) VALUES (%s, %s, %s, %s,"
                    " NULL, %s, %s, %s, %s, now(), now(), NULL)",
                    [
                        str(uuid4()),
                        self.staff["organization"],
                        clinic,
                        physician,
                        _utc(day, start),
                        _utc(day, end),
                        str(uuid4()),
                        secrets.token_bytes(32),
                    ],
                )
            connection.execute(
                "INSERT INTO clinic_app.scheduling_appointment (id, organization_id,"
                " clinic_id, patient_id, practitioner_id, start_at, end_at,"
                " idempotency_key, create_fingerprint, status, created_at,"
                " updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " 'scheduled', now(), now())",
                [
                    str(uuid4()),
                    self.staff["organization"],
                    self.staff["clinic_b"],
                    patient,
                    physician,
                    _utc(day, "14:00"),
                    _utc(day, "15:00"),
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
        return day, name

    def status(self, appointment: str) -> str:
        """Lifecycle state, read as the runtime role."""
        with psycopg.connect(self.staff["dsn"]) as connection:
            connection.execute("SET ROLE clinic_app")
            connection.execute(
                "SELECT set_config('app.current_tenant', %s, true),"
                " set_config('app.current_user_id', %s, true)",
                [self.staff["organization"], self.staff["receptionist_id"]],
            )
            row = connection.execute(
                "SELECT status FROM clinic_app.scheduling_appointment WHERE id = %s",
                [appointment],
            ).fetchone()
        assert row is not None
        return str(row[0])

    def holds_at(self, day: str, hhmm: str) -> int:
        """Holds starting at one clinic-local minute, read as the runtime role."""
        with psycopg.connect(self.staff["dsn"]) as connection:
            connection.execute("SET ROLE clinic_app")
            connection.execute(
                "SELECT set_config('app.current_tenant', %s, true),"
                " set_config('app.current_user_id', %s, true)",
                [self.staff["organization"], self.staff["receptionist_id"]],
            )
            row = connection.execute(
                "SELECT count(*) FROM clinic_app.scheduling_appointment"
                " WHERE status = 'held' AND start_at = %s",
                [_utc(day, hhmm)],
            ).fetchone()
        assert row is not None
        return int(row[0])


@dataclass(frozen=True, slots=True)
class Board:
    """The runner fixtures one board scene binds together."""

    base_url: str
    browser: Browser
    state: StorageState
    world: GridWorld
    root: Path
    report: dict[str, Any]


@pytest.fixture(scope="session")
def grid_world(agenda_staff: dict[str, str]) -> GridWorld:
    return GridWorld(agenda_staff)


@pytest.fixture(scope="session")
def grid_root(renewal_artifact_root: Path) -> Path:
    root = renewal_artifact_root / "agenda-grid"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def grid_report(grid_root: Path) -> Iterator[dict[str, Any]]:
    report: dict[str, Any] = {"engine": selected_engine(), "checks": {}}
    yield report
    path = grid_root / "agenda-grid-report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


@pytest.fixture(scope="session")
def grid_state(
    renewal_page: Page, renewal_base_url: str, agenda_staff: dict[str, str]
) -> StorageState:
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(locale="pt-BR") as context:
        page = context.new_page()
        _sign_in_receptionist(page, renewal_base_url, agenda_staff)
        return context.storage_state()


@pytest.fixture
def board(  # noqa: PLR0913 - the runner fixtures one scene binds together
    renewal_page: Page,
    renewal_base_url: str,
    grid_state: StorageState,
    grid_world: GridWorld,
    grid_root: Path,
    grid_report: dict[str, Any],
) -> Board:
    browser = renewal_page.context.browser
    assert browser is not None
    return Board(
        renewal_base_url, browser, grid_state, grid_world, grid_root, grid_report
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _url(world: GridWorld, view: str, day: str) -> str:
    clinic = world.staff["clinic_a"]
    if view == "day":
        return f"/scheduling/clinics/{clinic}/agenda/grid/{day}/"
    return f"/scheduling/clinics/{clinic}/agenda/board/{view}/{day}/"


def _open(page: Page, base_url: str, path: str) -> None:
    response = goto_settled(page, base_url + path)
    assert response is not None
    assert response.status == OK
    expect(page.locator("#agenda-grid-results")).to_have_count(1)
    # The stream's ready ACK immediately refetches the board to close the
    # subscription gap. Do not measure/capture a region it is about to replace.
    _connected(page)


def _open_native(page: Page, base_url: str, path: str) -> None:
    """Open a board page with scripts disabled (no settle probe can run)."""
    response = goto_settled(page, base_url + path)
    assert response is not None
    assert response.status == OK
    expect(page.locator("#agenda-grid-results")).to_have_count(1)


def _move_url(world: GridWorld) -> str:
    return f"/scheduling/clinics/{world.staff['clinic_a']}/agenda/grid/move/"


def _connected(page: Page) -> None:
    expect(page.locator("#agenda-shell")).to_have_attribute(
        "data-realtime-state", "connected"
    )
    wait_for_js(page, SETTLED_JS)


def _is_post(fragment: str) -> Callable[[Response], bool]:
    return lambda response: (
        response.request.method == "POST" and fragment in response.url
    )


def _is_refetch(day: str) -> Callable[[Response], bool]:
    return lambda response: (
        response.request.method == "GET"
        and "/agenda/grid/" in response.url
        and day in response.url
    )


def _is_options(query: str) -> Callable[[Response], bool]:
    def matches(response: Response) -> bool:
        data = response.request.post_data_json
        return (
            response.url.endswith("/api/ui/v1/command/search/")
            and response.request.method == "POST"
            and isinstance(data, dict)
            and str(data["q"]) == query
        )

    return matches


@contextmanager
def _own_move(page: Page, day: str) -> Iterator[None]:
    """A move this page makes: its answer, then its own realtime echo request."""
    with (
        page.expect_request(
            lambda request: (
                request.method == "GET"
                and "/agenda/grid/" in request.url
                and day in request.url
            )
        ),
        page.expect_response(_is_post("/agenda/grid/move/")) as moved,
    ):
        yield
    assert moved.value.status == OK
    wait_for_js(page, SETTLED_JS)


def _slot(page: Page, appointment: str) -> Locator:
    return page.locator(f'[data-move][data-appointment="{appointment}"]')


def _notice(page: Page) -> Locator:
    return page.locator("#agenda-grid-notice")


def _axe(page: Page, base_url: str, root: Path, name: str) -> list[dict[str, Any]]:
    if not evaluate_js(page, "typeof axe !== 'undefined'"):
        page.add_script_tag(url=f"{base_url}{AXE_URL}")
    violations: list[dict[str, Any]] = evaluate_js(page, AXE_RUN_JS)
    (root / f"axe-{name}.json").write_text(json.dumps(violations, indent=2))
    return violations


def _shot(page: Page, root: Path, name: str) -> str:
    path = root / f"{name}.png"
    page.screenshot(path=str(path))
    path.chmod(0o600)
    return path.name


def _board_shot(page: Page, root: Path, name: str) -> str:
    """Capture populated board content, not the shell or a table-row box."""
    row = page.locator(BOARD_CONTENT).first
    row.scroll_into_view_if_needed()
    expect(row).to_be_in_viewport()
    action_lines = evaluate_js(page, ACTION_LINES_JS)
    assert action_lines <= 2, (name, action_lines)
    return _shot(page, root, name)


def _dialog_captures(board: Board, page: Page, name: str) -> list[str]:
    """Check and capture every scroll segment of the actual open dialog."""
    box = page.locator("[data-move-dialog]")
    assert _axe(page, board.base_url, board.root, name) == []
    assert evaluate_js(page, TARGETS_JS) == [], name
    assert box.evaluate("(node) => node.scrollWidth <= node.clientWidth"), name
    height, extent = box.evaluate("(node) => [node.clientHeight, node.scrollHeight]")
    captures = []
    for index, top in enumerate(range(0, int(extent), int(height))):
        box.evaluate("(node, top) => { node.scrollTop = top; }", top)
        captures.append(_shot(page, board.root, f"{name}-{index}"))
    return captures


def _set_preferences(page: Page, base_url: str, theme: str, density: str) -> None:
    goto_settled(page, f"{base_url}/account/preferences/")
    page.locator(
        f'label.segmented-option:has(input[name="theme"][value="{theme}"])'
    ).click()
    page.locator(
        f'label.segmented-option:has(input[name="density"][value="{density}"])'
    ).click()
    with expect_document(page):
        page.locator('form.panel button[type="submit"]').click()


def _pin(page: Page, name: str) -> None:
    """Put a patient in context through Search, as reception does."""
    page.locator(".nav-command").click()
    expect(page.locator("#command-palette")).to_have_attribute("open", "")
    with page.expect_response(_is_options(name)) as found:
        page.locator("#command-palette-input").fill(name)
    assert found.value.status == OK
    option = page.locator("#command-palette-input-listbox [data-kind=patient]")
    expect(option).to_have_count(1)
    click_to_navigate(option)
    expect(page.locator("[data-patient-banner]")).to_contain_text(name)


# --------------------------------------------------------------------------
# 1. Every view, every width, light comfortable and dark compact: axe zero
# --------------------------------------------------------------------------


def test_every_view_is_axe_clean_with_44px_targets_at_every_width(
    board: Board,
) -> None:
    seeded = board.world.seed_day()
    results: dict[str, Any] = {}
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state
    ) as preferences:
        setter = preferences.new_page()
        try:
            for theme, density in PREFERENCES:
                _set_preferences(setter, board.base_url, theme, density)
                for width in WIDTHS:
                    size: ViewportSize = {"width": width, "height": 900}
                    with board.browser.new_context(
                        locale="pt-BR", storage_state=board.state, viewport=size
                    ) as context:
                        context.add_init_script(CSP_ONLY_JS)
                        page = context.new_page()
                        for view in VIEWS:
                            name = f"{view}-{width}-{theme}-{density}"
                            _open(
                                page,
                                board.base_url,
                                _url(board.world, view, seeded.day),
                            )
                            html = page.locator("html")
                            expect(html).to_have_attribute("data-theme", theme)
                            expect(html).to_have_attribute("data-density", density)
                            expect(
                                page.locator(".agenda-views a[aria-current=page]")
                            ).to_have_attribute(
                                "href", _url(board.world, view, seeded.day)
                            )
                            small = evaluate_js(page, TARGETS_JS)
                            overflow = evaluate_js(page, NO_OVERFLOW_JS)
                            violations = _axe(page, board.base_url, board.root, name)
                            csp = evaluate_js(page, "window.__slice.csp")
                            results[name] = {
                                "small_targets": small,
                                "no_overflow": overflow,
                                "axe": len(violations),
                                "csp": csp,
                                "max_action_line_boxes": evaluate_js(
                                    page, ACTION_LINES_JS
                                ),
                                "capture": _board_shot(page, board.root, name),
                            }
                            assert small == [], (name, small)
                            assert overflow, name
                            assert violations == [], (name, violations)
                            assert csp == 0, name
        finally:
            _set_preferences(setter, board.base_url, "light", "comfortable")
    board.report["checks"]["views"] = results


@pytest.mark.parametrize("scale", REFLOW_SCALES)
def test_board_zoom_reflow_and_reduced_motion(
    board: Board, scale: int, csp_console_violations: list[dict[str, str]]
) -> None:
    """1280x900 physical window, 200%/400% equivalent; no CSS zoom override."""
    seeded = board.world.seed_day()
    results: dict[str, Any] = {}
    size: ViewportSize = {
        "width": REFLOW_WINDOW[0] // scale,
        "height": REFLOW_WINDOW[1] // scale,
    }
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state
    ) as preferences:
        setter = preferences.new_page()
        try:
            for theme, density in PREFERENCES:
                _set_preferences(setter, board.base_url, theme, density)
                # The shared helper keeps Firefox's DPR across COOP navigations.
                with new_context(
                    board.browser,
                    locale="pt-BR",
                    storage_state=board.state,
                    viewport=size,
                    device_scale_factor=scale,
                    reduced_motion="reduce",
                ) as context:
                    page = context.new_page()
                    for view in VIEWS:
                        name = f"{view}-zoom-{scale * 100}-{theme}-{density}"
                        _open(page, board.base_url, _url(board.world, view, seeded.day))
                        metrics = evaluate_js(page, VIEWPORT_JS)
                        assert metrics == {
                            "css_width": size["width"],
                            "css_height": size["height"],
                            "device_pixel_ratio": scale,
                            "visual_viewport_scale": 1,
                            "root_font_size": "16px",
                            "reduced_motion": True,
                        }, metrics
                        expect(page.locator("html")).to_have_attribute(
                            "data-theme", theme
                        )
                        expect(page.locator("html")).to_have_attribute(
                            "data-density", density
                        )
                        captures = [
                            path.name
                            for path in full_page_screenshot(
                                page, board.root / f"{name}-full.png"
                            )
                        ]
                        captures.append(_board_shot(page, board.root, name))
                        assert evaluate_js(page, NO_OVERFLOW_JS), name
                        assert evaluate_js(page, TARGETS_JS) == [], name
                        motion = evaluate_js(page, MOTION_JS)
                        assert motion == [], (name, motion)
                        assert _axe(page, board.base_url, board.root, name) == []
                        dialogs: dict[str, list[str]] = {}
                        if view in {"day", "week"}:
                            appointment = page.locator("[data-move]").first
                            appointment_id = appointment.get_attribute(
                                "data-appointment"
                            )
                            assert appointment_id
                            appointment.focus()
                            page.keyboard.press("m")
                            box = page.locator("[data-move-dialog]")
                            expect(box).to_have_attribute("open", "")
                            expect(box.locator("[data-move-start]")).to_be_focused()
                            assert evaluate_js(page, MOTION_JS) == [], name
                            dialog_captures = _dialog_captures(
                                board, page, f"{name}-dialog"
                            )
                            page.keyboard.press("Tab")
                            assert evaluate_js(
                                page,
                                "!!document.activeElement.closest('[data-move-dialog]')",
                            )
                            page.keyboard.press("Escape")
                            expect(box).not_to_have_attribute("open", "")
                            wait_for_js(
                                page,
                                f"(id) => ({FOCUSED_APPOINTMENT_JS})() === id",
                                arg=appointment_id,
                            )
                            dialogs["move"] = dialog_captures
                        # The shared guard recognizes only WebKit's own
                        # per-frame screenshot stylesheet refusal, and still
                        # rejects application CSP violations during captures.
                        csp = len(csp_console_violations)
                        assert csp == 0, name
                        results[name] = {
                            "method": "halved/quartered CSS viewport plus device scale",
                            "physical_window": list(REFLOW_WINDOW),
                            "equivalent_zoom_percent": scale * 100,
                            "metrics": metrics,
                            "motion_offenders": motion,
                            "axe": 0,
                            "small_targets": [],
                            "no_overflow": True,
                            "csp": csp,
                            "max_action_line_boxes": evaluate_js(page, ACTION_LINES_JS),
                            "captures": captures,
                            "dialogs": dialogs,
                        }
        finally:
            _set_preferences(setter, board.base_url, "light", "comfortable")
    board.report["checks"][f"reflow_{scale * 100}"] = results


# --------------------------------------------------------------------------
# 2. Keyboard-only reschedule on the board (move.webm)
# --------------------------------------------------------------------------


def test_keyboard_reschedule_on_the_board(board: Board) -> None:
    seeded = board.world.seed_day()
    keys: list[str] = []
    size: ViewportSize = {"width": 1280, "height": 900}
    videos = board.root / "video-move"
    videos.mkdir(mode=0o700)

    def press(page: Page, key: str) -> None:
        keys.append(key)
        page.keyboard.press(key)

    context = board.browser.new_context(
        locale="pt-BR",
        storage_state=board.state,
        viewport=size,
        record_video_dir=str(videos),
        record_video_size=size,
    )
    try:
        page = context.new_page()
        _open(page, board.base_url, _url(board.world, "day", seeded.day))
        _connected(page)
        for _ in range(120):
            press(page, "Tab")
            if evaluate_js(
                page,
                "!!(document.activeElement && document.activeElement.closest"
                " && document.activeElement.closest('#agenda-grid'))",
            ):
                break
        else:
            pytest.fail("Tab never reached the board grid")
        for _ in range(4):
            if evaluate_js(page, FOCUSED_APPOINTMENT_JS):
                break
            press(page, "ArrowDown")
        appointment = evaluate_js(page, FOCUSED_APPOINTMENT_JS)
        assert appointment is not None
        before = board.world.stored(appointment)
        press(page, "m")
        expect(page.locator("[data-move-dialog]")).to_have_attribute("open", "")
        assert evaluate_js(page, "document.activeElement.matches('[data-move-start]')")
        press(page, "ArrowDown")
        chosen = evaluate_js(page, "document.activeElement.value")
        press(page, "Tab")
        press(page, "Tab")
        assert evaluate_js(
            page, "document.activeElement.matches('[data-move-confirm]')"
        )
        with _own_move(page, seeded.day):
            press(page, "Enter")
        expect(_notice(page)).to_contain_text(
            gettext("Appointment moved to %(start)s.") % {"start": chosen}
        )
        wait_for_js(
            page, f"(id) => ({FOCUSED_APPOINTMENT_JS})() === id", arg=appointment
        )
        after = board.world.stored(appointment)
        capture = _shot(page, board.root, "keyboard-move")
        video = page.video
    finally:
        context.close()
    assert video is not None
    video.save_as(str(board.root / "move.webm"))
    board.report["checks"]["keyboard_move"] = {
        "keys": keys,
        "before": before,
        "after": after,
        "capture": capture,
        "video": "move.webm",
    }
    assert after == (chosen, before[1] + 1)


# --------------------------------------------------------------------------
# 3. Target taken meanwhile: 409 with a worded conflict, original unchanged
# --------------------------------------------------------------------------


def test_a_target_taken_meanwhile_leaves_the_original_unchanged(
    board: Board,
) -> None:
    seeded: SliceDay = board.world.seed_day()
    physician = board.world.physicians[0]
    mover, rival = seeded.by_physician[physician][:2]
    target = seeded.free[physician][0]
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state
    ) as context:
        context.add_init_script(CSP_ONLY_JS)
        page, remote = context.new_page(), context.new_page()
        for each in (page, remote):
            _open(each, board.base_url, _url(board.world, "day", seeded.day))
        _connected(page)
        before = board.world.stored(mover)
        _slot(page, mover).click()
        expect(page.locator("[data-move-dialog]")).to_have_attribute("open", "")
        page.locator("[data-move-start]").select_option(target)
        rival_revision = board.world.stored(rival)[1]
        with page.expect_response(_is_refetch(seeded.day)):
            status = evaluate_js(
                remote,
                REMOTE_MOVE_JS,
                [
                    _move_url(board.world),
                    {
                        "appointment_id": rival,
                        "expected_revision": str(rival_revision),
                        "day": seeded.day,
                        "start": target,
                        "duration": "30",
                    },
                ],
            )
        assert status == OK
        with page.expect_response(_is_post("/agenda/grid/move/")) as lost:
            page.locator("[data-move-confirm]").click()
        assert lost.value.status == CONFLICT
        expect(_notice(page)).to_contain_text(gettext(RESOURCE_TEXT))
        expect(page.locator("[data-remote-change] .state-note")).to_have_count(1)
        capture = _shot(page, board.root, "taken-meanwhile")
        axe = _axe(page, board.base_url, board.root, "taken-meanwhile")
    after = board.world.stored(mover)
    board.report["checks"]["taken_meanwhile"] = {
        "before": before,
        "after": after,
        "status": lost.value.status,
        "capture": capture,
    }
    assert after == before
    assert axe == []


# --------------------------------------------------------------------------
# 4. Two receptionists, the last free room: one hold, one worded refusal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("initial", "intent"), [("held", "confirm"), ("arrived", "cancel")]
)
def test_action_only_dialog_shows_remote_conflict(
    board: Board,
    initial: str,
    intent: str,
    csp_console_violations: list[dict[str, str]],
) -> None:
    seeded = board.world.seed_day()
    appointment: str | None
    action_url = (
        f"/scheduling/clinics/{board.world.staff['clinic_a']}/agenda/grid/action/"
    )
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state
    ) as context:
        page, remote = context.new_page(), context.new_page()
        _open(page, board.base_url, _url(board.world, "day", seeded.day))
        if initial == "held":
            name = f"Sintética Reserva {_letters().title()}"
            board.world.patient(name)
            _pin(page, name)
            status = evaluate_js(
                page,
                REMOTE_MOVE_JS,
                [
                    action_url,
                    {
                        "intent": "hold",
                        "view": "day",
                        "day": seeded.day,
                        "practitioner": board.world.physicians[0],
                        "service": board.world.services["remote"],
                        "start": seeded.free[board.world.physicians[0]][0],
                        "duration": "30",
                        "idempotency_key": str(uuid4()),
                    },
                ],
            )
        else:
            appointment = seeded.by_physician[board.world.physicians[0]][0]
            status = evaluate_js(
                page,
                REMOTE_MOVE_JS,
                [
                    action_url,
                    {
                        "intent": "arrive",
                        "view": "day",
                        "day": seeded.day,
                        "appointment_id": appointment,
                        "expected_revision": str(board.world.stored(appointment)[1]),
                        "command_id": str(uuid4()),
                    },
                ],
            )
        assert status == OK
        for each in (page, remote):
            _open(each, board.base_url, _url(board.world, "day", seeded.day))
        _connected(page)
        opener = page.locator('button[type="button"].agenda-slot-actions').first
        appointment = opener.get_attribute("data-actions-for")
        revision = opener.get_attribute("data-revision")
        assert appointment
        assert revision
        assert board.world.status(appointment) == initial
        summary = (
            f"{opener.locator('strong').inner_text()} · "
            f"{opener.locator('.resource-slot-meta').inner_text()}"
        )
        opener.focus()
        page.keyboard.press("Space")
        box = page.locator("[data-move-dialog]")
        expect(box).to_have_attribute("open", "")
        expect(box).to_be_visible()
        expect(box.locator("[data-move-form]")).to_be_hidden()
        expect(box.locator("[data-move-find]")).to_be_hidden()
        expect(box.locator("#agenda-move-title")).to_be_visible()
        expect(box.locator("[data-move-summary]")).to_be_visible()
        expect(box.locator("[data-move-summary]")).to_have_text(summary)
        with page.expect_response(_is_refetch(seeded.day)) as refetched:
            status = evaluate_js(
                remote,
                REMOTE_MOVE_JS,
                [
                    action_url,
                    {
                        "intent": intent,
                        "view": "day",
                        "day": seeded.day,
                        "appointment_id": appointment,
                        "expected_revision": revision,
                        "command_id": str(uuid4()),
                    },
                ],
            )
        assert status == OK
        assert refetched.value.status == OK
        expect(box.locator("[data-move-conflict]")).to_be_visible()
        expect(box.locator("[data-move-summary]")).to_have_text(summary)
        expect(
            box.locator('[data-move-lifecycle] input[name="expected_revision"]')
        ).to_have_value(revision)
        assert board.world.stored(appointment)[1] == int(revision) + 1
        captures = _dialog_captures(board, page, f"{initial}-remote-conflict")
        page.keyboard.press("Tab")
        assert evaluate_js(
            page, "!!document.activeElement.closest('[data-move-dialog]')"
        )
        page.keyboard.press("Escape")
        expect(box).not_to_have_attribute("open", "")
        wait_for_js(
            page,
            "!!document.activeElement.matches('[data-grid-cell]') && "
            "!document.activeElement.closest('dialog')",
        )
        assert csp_console_violations == []
    board.report["checks"][f"{initial}_remote_conflict"] = {
        "initial_status": initial,
        "remote_action": intent,
        "revision_delta": 1,
        "captures": captures,
        "axe": 0,
        "focus_returned": True,
    }


def test_two_receptionists_racing_the_last_room_slot_hold_it_once(
    board: Board,
) -> None:
    seeded = board.world.seed_day()
    # 09:30: room 1 is free (physician 0 uses it on even slots) and room 2 is
    # busy (physician 1, odd slots), so room 1 is the last free room. The long
    # physician and physician 0 are both free then.
    slot = "09:30"
    room = board.world.rooms[0]
    names = (
        f"Sintética Recepção A {_letters().title()}",
        f"Sintética Recepção B {_letters().title()}",
    )
    for name in names:
        board.world.patient(name)
    second_desk = board.world.second_receptionist()
    size: ViewportSize = {"width": 1280, "height": 720}
    videos = board.root / "video-lastslot"
    videos.mkdir(mode=0o700)
    contexts: list[BrowserContext] = []
    try:
        for _ in range(2):
            context = board.browser.new_context(
                locale="pt-BR",
                viewport=size,
                record_video_dir=str(videos),
                record_video_size=size,
            )
            context.add_init_script(CSP_ONLY_JS)
            contexts.append(context)
        first, second = (context.new_page() for context in contexts)
        _sign_in_receptionist(first, board.base_url, board.world.staff)
        _sign_in(second, board.base_url, second_desk, board.world.staff["password"])
        wait_for_signed_in(second)
        for page, name, professional in (
            (first, names[0], board.world.physicians[2]),
            (second, names[1], board.world.physicians[0]),
        ):
            # Search runs in the clinic the shell last showed; a desk holding
            # roles in two clinics would otherwise search its first clinic.
            _open(page, board.base_url, _url(board.world, "day", seeded.day))
            _pin(page, name)
            _open(page, board.base_url, _url(board.world, "day", seeded.day))
            _connected(page)
            page.locator(
                f'[data-grid-cell][data-book][data-lane="{room}"][data-slot="{slot}"]'
            ).click()
            expect(page.locator("[data-book-dialog]")).to_have_attribute("open", "")
            expect(
                page.locator(f'[data-book-resource][value="{room}"]')
            ).to_be_checked()
            page.locator("#agenda-book-professional").select_option(professional)
            page.locator("#agenda-book-service").select_option(
                board.world.services["room"]
            )
        with (
            second.expect_response(_is_refetch(seeded.day)),
            first.expect_response(_is_post("/agenda/grid/action/")) as won,
        ):
            first.locator("[data-book-confirm]").click()
        assert won.value.status == OK
        expect(first.locator(".agenda-holds")).to_contain_text(names[0])
        with second.expect_response(_is_post("/agenda/grid/action/")) as lost:
            second.locator("[data-book-confirm]").click()
        assert lost.value.status == CONFLICT
        expect(_notice(second)).to_contain_text(gettext(TAKEN_TEXT))
        expect(second.locator("[data-book-dialog]")).not_to_have_attribute("open", "")
        holds = board.world.holds_at(seeded.day, slot)
        loser_capture = _shot(second, board.root, "last-room-loser")
        winner_capture = _shot(first, board.root, "last-room-winner")
        loser_video = second.video
        winner_video = first.video
    finally:
        for context in contexts:
            context.close()
    assert loser_video is not None
    assert winner_video is not None
    loser_video.save_as(str(board.root / "lastslot.webm"))
    winner_video.save_as(str(board.root / "lastslot-winner.webm"))
    board.report["checks"]["last_room_slot"] = {
        "slot": slot,
        "holds": holds,
        "winner_status": won.value.status,
        "loser_status": lost.value.status,
        "captures": [winner_capture, loser_capture],
        "video": "lastslot.webm",
    }
    assert holds == 1


# --------------------------------------------------------------------------
# 5. Filters, saved views and the month drill
# --------------------------------------------------------------------------


def test_filters_saved_views_and_the_month_drill(board: Board) -> None:
    seeded = board.world.seed_day()
    professional = board.world.physicians[1]
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state
    ) as context:
        page = context.new_page()
        _open(page, board.base_url, _url(board.world, "week", seeded.day))
        page.locator("#agenda-filter-professional").select_option(professional)
        with expect_document(page):
            page.locator('.agenda-filters button[value="filter"]').click()
        label = page.locator(
            f'#agenda-filter-professional option[value="{professional}"]'
        ).text_content()
        assert label
        expect(page.locator("#agenda-grid-caption")).to_contain_text(label.strip())
        first_id = seeded.by_physician[professional][0]
        expect(page.locator(f'[data-appointment="{first_id}"]')).to_have_count(1)
        page.locator(".agenda-saved > summary").click()
        with expect_document(page):
            page.locator('.agenda-saved button[value="save"]').click()
        page.locator(".agenda-saved > summary").click()
        expect(page.locator(".agenda-saved-label")).to_contain_text(gettext("Week"))
        with expect_document(page):
            page.locator('.agenda-filters button[value="clear"]').click()
        _open(page, board.base_url, _url(board.world, "month", seeded.day))
        day_link = page.locator(f'.agenda-month-link time[datetime="{seeded.day}"]')
        expect(day_link).to_have_count(1)
        with expect_document(page):
            day_link.click()
        assert page.url.endswith(_url(board.world, "day", seeded.day))
        page.locator(".agenda-saved > summary").click()
        with expect_document(page):
            page.locator('.agenda-saved button[value="open"]').first.click()
        assert page.url.endswith(_url(board.world, "week", seeded.day))
        expect(page.locator("#agenda-filter-professional")).to_have_value(professional)
        page.locator(".agenda-saved > summary").click()
        with expect_document(page):
            page.locator('.agenda-saved button[value="archive"]').first.click()
        with expect_document(page):
            page.locator('.agenda-filters button[value="clear"]').click()
        capture = _shot(page, board.root, "filters-saved")
    board.report["checks"]["filters_saved_month"] = {"capture": capture}


# --------------------------------------------------------------------------
# 6. Another unit's busy time and the remote-change banner
# --------------------------------------------------------------------------


def test_other_unit_busy_time_names_no_patient(board: Board) -> None:
    day, other = board.world.other_unit_booking()
    physician = board.world.staff["physician_a_id"]
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state
    ) as context:
        page = context.new_page()
        _open(page, board.base_url, _url(board.world, "day", day))
        cell = page.locator(
            f'[data-grid-cell][data-column="{physician}"][data-slot="14:00"]'
        )
        expect(cell).to_contain_text(gettext("Busy (other unit)"))
        expect(cell).not_to_have_attribute("data-book", "")
        assert other not in page.content()
        capture = _shot(page, board.root, "other-unit")
    board.report["checks"]["other_unit"] = {"capture": capture}


# --------------------------------------------------------------------------
# 7. JavaScript off: list actions and the slot finder stay native POSTs
# --------------------------------------------------------------------------


def test_js_off_list_actions_and_slot_finder_post_body_selectors(
    board: Board,
) -> None:
    seeded = board.world.seed_day()
    physician = board.world.physicians[0]
    arriving, moving = seeded.by_physician[physician][:2]
    visited: list[str] = []
    with board.browser.new_context(
        locale="pt-BR", storage_state=board.state, java_script_enabled=False
    ) as context:
        page = context.new_page()
        page.on("request", lambda request: visited.append(request.url))
        _open_native(page, board.base_url, _url(board.world, "list", seeded.day))
        row = page.locator(f'.agenda-list-lifecycle:has(input[value="{arriving}"])')
        with expect_document(page):
            row.locator('button[value="arrive"]').click()
        assert page.url.endswith(_url(board.world, "list", seeded.day))
        expect(page.locator(".state-note--success")).to_contain_text(
            gettext("Arrival recorded.")
        )
        move = page.locator(f'.agenda-list-move:has(button[value="{moving}"])')
        with expect_document(page):
            move.locator("button").click()
        expect(page.locator("[data-native-move-form]")).to_have_count(1)
        with expect_document(page):
            page.locator('.agenda-move-find button[type="submit"]').click()
        expect(page.locator("#agenda-move-start option")).not_to_have_count(0)
        capture = _shot(page, board.root, "js-off-finder")
    assert board.world.status(arriving) == "arrived"
    assert not any(arriving in url or moving in url for url in visited)
    board.report["checks"]["js_off"] = {"capture": capture}
