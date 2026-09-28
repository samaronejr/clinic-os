"""Todo 23 agenda vertical slice: ADR-001's criteria, measured per variant.

The multi-resource day view (three physicians, two rooms, 40 synthetic
appointments per day) is served as ``clinic_app`` with realtime on. Every
variant in ``VARIANTS`` renders the same DOM contract (``data-move``,
``data-grid-cell``, ``[data-move-dialog]``, ``#agenda-grid-notice``), so one
script measures each of them the same way:

* INP p75 under a Moto-G-class profile at 4x CPU (web-vitals ``onINP`` plus
  raw Event Timing entries) over ``INP_RUNS`` fresh page loads, each with the
  same open-dialog, keyboard-move, arrow, drag, M-key and realtime-refetch
  interactions;
* gzipped JS the variant adds over the existing agenda page;
* a keyboard-only reschedule;
* axe (zero violations of any impact) and 44px targets at 1280/375/320;
* two contexts moving one appointment: conflict text, no lost update, video;
* the JS-off grid (a native ledger of reschedule links);
* strict CSP: zero ``securitypolicyviolation`` events.

A run over budget is recorded as failing in ``slice-<variant>.json`` and then
fails its test, so the report always carries the measured numbers. Each
scene works on its own civil day; every wait subscribes to a response, a
DOM state or a realtime event, never a timer.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._page_wait import evaluate_js, wait_for_js
from renewal.browser.a11y_support import AXE_RUN_JS, AXE_URL
from renewal.browser.engines import selected_engine, throttle_cpu
from renewal.browser.test_agenda import (
    SETTLED_JS,
    _enrol,
    _sign_in_receptionist,
    _utc,
    agenda_staff,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        Response,
        StorageState,
        ViewportSize,
    )

__all__ = ("agenda_staff",)


@dataclass(frozen=True, slots=True)
class SliceVariant:
    """Where a variant lives and which requests prove its work finished."""

    route: str
    move: str
    refetch: str
    refetch_method: str
    ready: str
    js_budget_kb: float


VARIANTS: Final = {
    "htmx": SliceVariant(
        route="grid",
        move="/agenda/grid/move/",
        refetch="/agenda/grid/",
        refetch_method="GET",
        ready="#agenda-grid[data-enhanced]",
        js_budget_kb=15.0,
    ),
}
INP_RUNS: Final = 7
# Lighthouse's mobile emulation (moto g power 2022, lighthouse-core
# config/constants.js screenEmulation.mobile + userAgents.mobile): a
# Moto-G-class screen, touch and user agent; the CPU is slowed 4x below.
MOTO_G_POWER: Final = {
    "viewport": {"width": 412, "height": 823},
    "device_scale_factor": 1.75,
    "is_mobile": True,
    "has_touch": True,
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
    ),
}
CPU_RATE: Final = 4
INP_BUDGET_MS: Final = 200.0
ROOMS: Final = ("Sala Sintética 1", "Sala Sintética 2")
WEB_VITALS: Final = Path(__file__).with_name("vendor") / "web-vitals"
WEB_VITALS_SHA256: Final = (
    "1e5e9b9af6b8d71cfef508e5a869c53b4cf2bbccad8c9b5ac664c34e93f6151a"
)
MIN_TARGET_PX: Final = 44
SLOTS: Final = 26  # 07:00-20:00 in half hours
RUN_SHIFT: Final = timedelta(days=7 * secrets.randbelow(150))
INP_COLLECT_JS: Final = """
(() => {
  window.__slice = {inp: null, events: [], csp: 0};
  document.addEventListener('securitypolicyviolation', () => {
    window.__slice.csp += 1;
  });
  webVitals.onINP((metric) => {
    window.__slice.inp = {value: metric.value, entries: metric.entries.length};
  }, {reportAllChanges: true, durationThreshold: 16});
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!entry.interactionId) continue;
      window.__slice.events.push({
        name: entry.name, interaction: entry.interactionId,
        duration: entry.duration,
        input_delay: entry.processingStart - entry.startTime,
        processing: entry.processingEnd - entry.processingStart,
      });
    }
  }).observe({type: 'event', durationThreshold: 16, buffered: true});
})();
"""
CSP_ONLY_JS: Final = """
(() => {
  window.__slice = {csp: 0};
  document.addEventListener('securitypolicyviolation', () => {
    window.__slice.csp += 1;
  });
})();
"""
TARGETS_JS: Final = """() => [...document.querySelectorAll(
  'main a, main button, main select, main input:not([type=hidden]), main summary')]
  .filter((node) => {
    const rect = node.getBoundingClientRect();
    const dialog = node.closest('dialog');
    return rect.width > 0 && rect.height > 0 && (!dialog || dialog.open)
      && (rect.width < 44 || rect.height < 44);
  }).map((node) => node.outerHTML.slice(0, 80))"""
REMOTE_MOVE_JS: Final = """async ([url, fields]) => {
  const body = new URLSearchParams(fields);
  body.set('csrfmiddlewaretoken',
    document.querySelector('[name=csrfmiddlewaretoken]').value);
  const response = await fetch(url, {method: 'POST', body,
    headers: {'HX-Request': 'true'}});
  return response.status;
}"""
FOCUSED_APPOINTMENT_JS: Final = """() => {
  const cell = document.activeElement && document.activeElement.closest
    && document.activeElement.closest('[data-grid-cell]');
  const link = cell && cell.querySelector('[data-appointment]');
  return link ? link.getAttribute('data-appointment') : null;
}"""


def _variants() -> list[str]:
    return list(VARIANTS)


# --------------------------------------------------------------------------
# Fixture world
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SliceDay:
    """One seeded civil day: appointment ids by physician, in slot order."""

    day: str
    by_physician: dict[str, list[str]]
    free: dict[str, list[str]]


class SliceWorld:
    """Synthetic clinic resources plus a fresh 40-appointment day per scene."""

    def __init__(self, staff: dict[str, str]) -> None:
        self.staff = staff
        self.physicians = (
            staff["physician_a_id"],
            staff["physician_b_id"],
            staff["physician_long_id"],
        )
        self.rooms: list[str] = []
        self.services: dict[str, str] = {}
        self.next_day = date(2033, 3, 1) + RUN_SHIFT
        with self._owner() as connection:
            for name in ROOMS:
                room = str(uuid4())
                connection.execute(
                    "INSERT INTO clinic_app.scheduling_resource (id, organization_id,"
                    " clinic_id, kind, name, capacity, active)"
                    " VALUES (%s, %s, %s, 'room', %s, 1, true)",
                    [room, staff["organization"], staff["clinic_a"], name],
                )
                self.rooms.append(room)
            for key, name, kinds in (
                ("room", "Consulta sintética", ["room"]),
                ("remote", "Teleconsulta sintética", []),
            ):
                service = str(uuid4())
                connection.execute(
                    "INSERT INTO clinic_app.scheduling_servicetype (id,"
                    " organization_id, clinic_id, name, duration_min, buffer_before,"
                    " buffer_after, required_professional_roles,"
                    " required_resource_kinds, insurer_billable, price_ref, active)"
                    " VALUES (%s, %s, %s, %s, 30, 0, 0, '{physician}', %s, false,"
                    " '', true)",
                    [service, staff["organization"], staff["clinic_a"], name, kinds],
                )
                self.services[key] = service

    def _owner(self) -> psycopg.Connection[Any]:
        connection = psycopg.connect(self.staff["dsn"])
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, false),"
            " set_config('app.current_user_id', %s, false)",
            [self.staff["organization"], self.staff["receptionist_id"]],
        )
        return connection

    def _block(
        self, connection: psycopg.Connection[Any], day: str, subject: str, room: bool
    ) -> None:
        connection.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock (id, organization_id,"
            " clinic_id, practitioner_id, resource_id, start_at, end_at,"
            " idempotency_key, create_fingerprint, created_at, updated_at,"
            " retired_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(),"
            " NULL)",
            [
                str(uuid4()),
                self.staff["organization"],
                self.staff["clinic_a"],
                None if room else subject,
                subject if room else None,
                _utc(day, "07:00"),
                _utc(day, "20:00"),
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )

    def seed_day(self) -> SliceDay:
        """Seed 40 appointments: two physicians alternate rooms, one is remote."""
        day = self.next_day.isoformat()
        self.next_day += timedelta(days=1)
        slots = [
            f"{7 + index // 2:02d}:{30 * (index % 2):02d}" for index in range(SLOTS)
        ]
        plan: dict[str, list[int]] = {
            self.physicians[0]: [k for k in range(SLOTS) if k % 2 == 0],
            self.physicians[1]: [k for k in range(SLOTS) if k % 2 == 1],
            self.physicians[2]: [k for k in range(21) if k % 3 != 2],
        }
        by_physician: dict[str, list[str]] = {}
        with self._owner() as connection:
            for subject in self.physicians:
                self._block(connection, day, subject, room=False)
            for room in self.rooms:
                self._block(connection, day, room, room=True)
            for index, (physician, taken) in enumerate(plan.items()):
                assigned = self.rooms[index] if index < len(self.rooms) else None
                ids = []
                for k in taken:
                    name = f"Sintético Paciente {index}-{k:02d}"
                    enrollment = _enrol(connection, self.staff, name, "clinic_a")
                    row = connection.execute(
                        "SELECT patient_id FROM"
                        " clinic_app.intake_patientclinicenrollment WHERE id = %s",
                        [enrollment],
                    ).fetchone()
                    assert row is not None
                    appointment = str(uuid4())
                    start = slots[k]
                    end = slots[k + 1] if k + 1 < SLOTS else "20:00"
                    connection.execute(
                        "INSERT INTO clinic_app.scheduling_appointment (id,"
                        " organization_id, clinic_id, patient_id, practitioner_id,"
                        " start_at, end_at, idempotency_key, create_fingerprint,"
                        " status, service_type_id, resource_ids, created_at,"
                        " updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                        " 'scheduled', %s, %s, now(), now())",
                        [
                            appointment,
                            self.staff["organization"],
                            self.staff["clinic_a"],
                            row[0],
                            physician,
                            _utc(day, start),
                            _utc(day, end),
                            str(uuid4()),
                            secrets.token_bytes(32),
                            self.services["room" if assigned else "remote"],
                            [assigned] if assigned else [],
                        ],
                    )
                    ids.append(appointment)
                by_physician[physician] = ids
        free = {
            physician: [slots[k] for k in range(SLOTS) if k not in taken]
            for physician, taken in plan.items()
        }
        # Physician 0 uses room 1 on even slots, so its odd slots are free in
        # both its column and its room; physician 1 mirrors it on room 2.
        return SliceDay(day=day, by_physician=by_physician, free=free)

    def stored(self, appointment: str) -> tuple[str, int]:
        """Clinic-local start and revision, read as the runtime role."""
        with psycopg.connect(self.staff["dsn"]) as connection:
            connection.execute("SET ROLE clinic_app")
            connection.execute(
                "SELECT set_config('app.current_tenant', %s, true),"
                " set_config('app.current_user_id', %s, true)",
                [self.staff["organization"], self.staff["receptionist_id"]],
            )
            row = connection.execute(
                "SELECT to_char(start_at AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI'),"
                " revision FROM clinic_app.scheduling_appointment WHERE id = %s",
                [appointment],
            ).fetchone()
        assert row is not None
        return str(row[0]), int(row[1])


@pytest.fixture(scope="session")
def slice_world(agenda_staff: dict[str, str]) -> SliceWorld:
    return SliceWorld(agenda_staff)


@pytest.fixture(scope="session")
def slice_root(renewal_artifact_root: Path) -> Path:
    root = renewal_artifact_root / "agenda-slice"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def slice_report(slice_root: Path) -> Iterator[dict[str, dict[str, Any]]]:
    """Collect every measurement; written even when a budget failed a test."""
    report: dict[str, dict[str, Any]] = {name: {} for name in VARIANTS}
    yield report
    for name, metrics in report.items():
        path = slice_root / f"slice-{name}.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "clinic-os/agenda-slice-variant/v1",
                    "variant": name,
                    "engine": selected_engine(),
                    "metrics": metrics,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        path.chmod(0o600)


@pytest.fixture(scope="session")
def signed_in_state(
    renewal_page: Page, renewal_base_url: str, agenda_staff: dict[str, str]
) -> StorageState:
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(locale="pt-BR") as context:
        page = context.new_page()
        _sign_in_receptionist(page, renewal_base_url, agenda_staff)
        return context.storage_state()


@dataclass(frozen=True, slots=True)
class Scene:
    """Everything one slice scene needs: server, browser, session and report."""

    base_url: str
    browser: Browser
    state: StorageState
    world: SliceWorld
    root: Path
    report: dict[str, dict[str, Any]]


@pytest.fixture
def scene(  # noqa: PLR0913 - the runner fixtures one scene binds together
    renewal_page: Page,
    renewal_base_url: str,
    signed_in_state: StorageState,
    slice_world: SliceWorld,
    slice_root: Path,
    slice_report: dict[str, dict[str, Any]],
) -> Scene:
    browser = renewal_page.context.browser
    assert browser is not None
    return Scene(
        base_url=renewal_base_url,
        browser=browser,
        state=signed_in_state,
        world=slice_world,
        root=slice_root,
        report=slice_report,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _path(world: SliceWorld, variant: str, day: str) -> str:
    route = VARIANTS[variant].route
    return f"/scheduling/clinics/{world.staff['clinic_a']}/agenda/{route}/{day}/"


def _open(page: Page, base_url: str, path: str, variant: str) -> None:
    response = page.goto(base_url + path)
    assert response is not None
    assert response.status == 200
    expect(page.locator(VARIANTS[variant].ready)).to_have_count(1)
    wait_for_js(page, SETTLED_JS)


def _connected(page: Page) -> None:
    """Realtime is up and its connect-time reconciliation refetch has landed."""
    expect(page.locator("#agenda-shell")).to_have_attribute(
        "data-realtime-state", "connected"
    )
    wait_for_js(page, SETTLED_JS)


def _is_move(variant: str) -> Any:  # noqa: ANN401 - Playwright predicate
    marker = VARIANTS[variant].move
    return lambda response: response.request.method == "POST" and marker in response.url


def _is_refetch(variant: str, day: str) -> Any:  # noqa: ANN401 - predicate
    selected = VARIANTS[variant]
    return lambda response: (
        selected.refetch in response.url
        and response.request.method == selected.refetch_method
        and (selected.refetch_method == "POST" or day in response.url)
    )


@contextmanager
def _own_move(page: Page, variant: str, day: str) -> Iterator[None]:
    """Wrap a move this page makes: its answer, then its own realtime echo.

    Every committed move publishes an agenda hint to every subscriber,
    including the author, whose grid then refetches once more. The hint can
    land before the move's own answer, and HTMX then aborts that refetch when
    the answer replaces the grid, so the echo is awaited as a sent request
    (answered or aborted) and the grid as settled, never as a response.
    """
    selected = VARIANTS[variant]
    with (
        page.expect_request(
            lambda request: (
                selected.refetch in request.url
                and request.method == selected.refetch_method
                and (selected.refetch_method == "POST" or day in request.url)
            )
        ),
        page.expect_response(_is_move(variant)) as moved,
    ):
        yield
    assert moved.value.status == 200
    wait_for_js(page, SETTLED_JS)


def _slot_link(page: Page, appointment: str) -> Any:  # noqa: ANN401 - Locator
    return page.locator(f'[data-move][data-appointment="{appointment}"]')


def _cell(page: Page, physician: str, slot: str) -> Any:  # noqa: ANN401 - Locator
    return page.locator(
        f'[data-grid-cell][data-column="{physician}"][data-slot="{slot}"]'
    )


def _notice(page: Page) -> Any:  # noqa: ANN401 - Locator
    return page.locator("#agenda-grid-notice")


def _drag(page: Page, appointment: str, physician: str, slot: str) -> None:
    source = _slot_link(page, appointment)
    target = _cell(page, physician, slot)
    source.scroll_into_view_if_needed()
    start = source.bounding_box()
    assert start is not None
    page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
    page.mouse.down()
    target.scroll_into_view_if_needed()
    end = target.bounding_box()
    assert end is not None
    page.mouse.move(end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=8)
    expect(target).to_have_class(re.compile(r"\bis-drop-target\b"))
    page.mouse.up()


def _p75(values: list[float]) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(0.75 * len(ordered)))
    return ordered[rank - 1]


def _gz(body: bytes) -> int:
    return len(gzip.compress(body, compresslevel=9, mtime=0))


# --------------------------------------------------------------------------
# 1. INP p75, Moto-G-class profile, 4x CPU
# --------------------------------------------------------------------------


def _interactions(
    page: Page,
    remote: Page,
    variant: str,
    world: SliceWorld,
    seeded: SliceDay,
) -> dict[str, float]:
    """The same scripted interactions for every run and variant."""
    timings: dict[str, float] = {}
    first, second = world.physicians[0], world.physicians[1]
    a0, a1, a2 = seeded.by_physician[first][:3]
    # a. open the move dialog, pick the next time by keyboard, confirm.
    _slot_link(page, a0).click()
    expect(page.locator("[data-move-dialog]")).to_have_attribute("open", "")
    page.keyboard.press("ArrowDown")
    with _own_move(page, variant, seeded.day):
        page.locator("[data-move-confirm]").click()
    expect(_notice(page)).to_be_visible()
    # b. arrows across the grid from the moved appointment.
    for key in ("ArrowRight", "ArrowRight", "ArrowDown", "ArrowDown", "ArrowLeft"):
        page.keyboard.press(key)
    # c. drag a1 onto physician 0's next free (odd) slot.
    target = seeded.free[first][3]
    with _own_move(page, variant, seeded.day):
        _drag(page, a1, first, target)
    expect(_slot_link(page, a1)).to_have_attribute("data-start", target)
    # d. M on a2 opens the dialog; Escape closes it.
    _slot_link(page, a2).focus()
    page.keyboard.press("m")
    expect(page.locator("[data-move-dialog]")).to_have_attribute("open", "")
    page.keyboard.press("Escape")
    expect(page.locator("[data-move-dialog]")).not_to_have_attribute("open", "")
    # e. another session moves physician 1's first appointment; this page
    #    refetches over SSE, then the user clicks straight away.
    b0 = seeded.by_physician[second][0]
    stored_revision = world.stored(b0)[1]
    evaluate_js(page, "window.__sliceRemote = performance.now()")
    with page.expect_response(_is_refetch(variant, seeded.day)) as refetched:
        status = evaluate_js(
            remote,
            REMOTE_MOVE_JS,
            [
                f"/scheduling/clinics/{world.staff['clinic_a']}/agenda/grid/move/",
                {
                    "appointment_id": b0,
                    "expected_revision": str(stored_revision),
                    "day": seeded.day,
                    "start": seeded.free[second][5],
                    "duration": "30",
                },
            ],
        )
        assert status == 200
    assert refetched.value.status == 200
    expect(page.locator(f'[data-appointment="{b0}"]').first).to_have_attribute(
        "data-start", seeded.free[second][5]
    )
    timings["realtime_refetch_ms"] = float(
        evaluate_js(page, "performance.now() - window.__sliceRemote")
    )
    _slot_link(page, a2).click()
    expect(page.locator("[data-move-dialog]")).to_have_attribute("open", "")
    page.keyboard.press("Escape")
    return timings


# INP needs Chromium's Event Timing and DevTools CPU throttling (Moto-G-class
# 4x). On Firefox and WebKit legs the INP scene is not applicable, so it is
# not defined there (never an empty, skipped parametrization); every other
# scene still runs on every engine.
if selected_engine() == "chromium":

    @pytest.mark.parametrize("variant", _variants())
    def test_inp_p75_under_4x_cpu_on_a_moto_g_profile(
        variant: str, scene: Scene
    ) -> None:
        source = (WEB_VITALS / "web-vitals.iife.js").read_bytes()
        assert hashlib.sha256(source).hexdigest() == WEB_VITALS_SHA256
        browser = scene.browser
        runs: list[dict[str, Any]] = []
        for _ in range(INP_RUNS):
            seeded = scene.world.seed_day()
            context = browser.new_context(
                locale="pt-BR",
                viewport={"width": 412, "height": 823},
                device_scale_factor=1.75,
                is_mobile=True,
                has_touch=True,
                user_agent=str(MOTO_G_POWER["user_agent"]),
            )
            context.add_cookies(
                [
                    {
                        "name": cookie["name"],
                        "value": cookie["value"],
                        "domain": cookie["domain"],
                        "path": cookie["path"],
                        "httpOnly": cookie["httpOnly"],
                        "secure": cookie["secure"],
                        "sameSite": cookie["sameSite"],
                    }
                    for cookie in scene.state["cookies"]
                ]
            )
            context.add_init_script(source.decode() + INP_COLLECT_JS)
            try:
                remote = context.new_page()
                page = context.new_page()
                page.set_default_timeout(30_000)
                _open(
                    remote,
                    scene.base_url,
                    _path(scene.world, "htmx", seeded.day),
                    "htmx",
                )
                _open(
                    page,
                    scene.base_url,
                    _path(scene.world, variant, seeded.day),
                    variant,
                )
                _connected(page)
                throttle = throttle_cpu(page, CPU_RATE)
                timings = _interactions(page, remote, variant, scene.world, seeded)
                collected = evaluate_js(page, "window.__slice")
            finally:
                context.close()
            by_interaction: dict[int, float] = {}
            for event in collected["events"]:
                key = int(event["interaction"])
                by_interaction[key] = max(
                    by_interaction.get(key, 0.0), event["duration"]
                )
            inp = collected["inp"]
            runs.append(
                {
                    "day": seeded.day,
                    "web_vitals_inp_ms": None if inp is None else inp["value"],
                    "interactions_over_16ms": sorted(by_interaction.values()),
                    "event_entries": collected["events"],
                    "csp_violations": collected["csp"],
                    "throttle": throttle,
                    **timings,
                }
            )
        # An interaction under 16 ms never reaches Event Timing; count it as 16.
        values = [
            run["web_vitals_inp_ms"] if run["web_vitals_inp_ms"] is not None else 16.0
            for run in runs
        ]
        p75 = _p75(values)
        scene.report[variant]["inp"] = {
            "criterion": "INP p75 < 200 ms, Moto G Power profile, 4x CPU",
            "profile": MOTO_G_POWER,
            "cpu_throttle_rate": CPU_RATE,
            "runs": len(runs),
            "p75_ms": p75,
            "budget_ms": INP_BUDGET_MS,
            "pass": p75 < INP_BUDGET_MS,
            "raw": runs,
        }
        assert all(run["csp_violations"] == 0 for run in runs)
        assert p75 < INP_BUDGET_MS, (variant, p75, values)


# --------------------------------------------------------------------------
# 2. Gzipped JavaScript the variant adds over the agenda ledger
# --------------------------------------------------------------------------


def _scripts(
    page: Page, base_url: str, path: str, variant: str | None
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def record(response: Response) -> None:
        if response.request.resource_type != "script":
            return
        body = response.body()
        seen[urlsplit(response.url).path] = {
            "bytes": len(body),
            "gzip_bytes": _gz(body),
            "content_encoding": response.headers.get("content-encoding", "identity"),
        }

    page.on("response", record)
    if variant is None:
        response = page.goto(base_url + path)
        assert response is not None
        assert response.status == 200
        wait_for_js(page, SETTLED_JS)
    else:
        _open(page, base_url, path, variant)
    page.remove_listener("response", record)
    return seen


@pytest.mark.parametrize("variant", _variants())
def test_gzipped_js_added_over_the_agenda_ledger(variant: str, scene: Scene) -> None:
    seeded = scene.world.seed_day()
    browser = scene.browser
    with browser.new_context(
        locale="pt-BR", storage_state=scene.state, service_workers="block"
    ) as context:
        page = context.new_page()
        ledger = _scripts(
            page,
            scene.base_url,
            f"/scheduling/clinics/{scene.world.staff['clinic_a']}/agenda/day/"
            f"{seeded.day}/1/",
            None,
        )
    with browser.new_context(
        locale="pt-BR", storage_state=scene.state, service_workers="block"
    ) as context:
        page = context.new_page()
        scripts = _scripts(
            page, scene.base_url, _path(scene.world, variant, seeded.day), variant
        )
    added = {path: sizes for path, sizes in scripts.items() if path not in ledger}
    added_kb = sum(item["gzip_bytes"] for item in added.values()) / 1024
    budget = VARIANTS[variant].js_budget_kb
    scene.report[variant]["js_gzip"] = {
        "criterion": f"gzipped JS added over the agenda ledger < {budget:g} KB",
        "gzip": "python gzip level 9 of each script body (server sends identity)",
        "added_kb": round(added_kb, 2),
        "total_page_kb": round(
            sum(item["gzip_bytes"] for item in scripts.values()) / 1024, 2
        ),
        "budget_kb": budget,
        "pass": added_kb < budget,
        "raw": {"ledger": ledger, "variant": scripts, "added": added},
    }
    assert added_kb < budget, (variant, added_kb, added)


# --------------------------------------------------------------------------
# 3. Keyboard-only reschedule
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _variants())
def test_keyboard_only_reschedule(variant: str, scene: Scene) -> None:
    seeded = scene.world.seed_day()
    browser = scene.browser
    keys: list[str] = []

    def press(page: Page, key: str) -> None:
        keys.append(key)
        page.keyboard.press(key)

    with browser.new_context(
        locale="pt-BR",
        storage_state=scene.state,
        viewport={"width": 1280, "height": 900},
    ) as context:
        context.add_init_script(CSP_ONLY_JS)
        page = context.new_page()
        _open(page, scene.base_url, _path(scene.world, variant, seeded.day), variant)
        _connected(page)
        for _ in range(80):
            press(page, "Tab")
            if evaluate_js(
                page,
                "!!(document.activeElement && document.activeElement.closest"
                " && document.activeElement.closest('#agenda-grid'))",
            ):
                break
        else:
            pytest.fail("Tab never reached the grid")
        for _ in range(4):
            if evaluate_js(page, FOCUSED_APPOINTMENT_JS):
                break
            press(page, "ArrowDown")
        appointment = evaluate_js(page, FOCUSED_APPOINTMENT_JS)
        assert appointment is not None
        before = scene.world.stored(appointment)
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
        with _own_move(page, variant, seeded.day):
            press(page, "Enter")
        expect(_notice(page)).to_contain_text(
            gettext("Appointment moved to %(start)s.") % {"start": chosen}
        )
        wait_for_js(
            page,
            f"(id) => ({FOCUSED_APPOINTMENT_JS})() === id",
            arg=appointment,
        )
        after = scene.world.stored(appointment)
        # Before any capture: WebKit's screenshotter injects a style the CSP
        # refuses (conftest.py), which is not the page's own violation.
        csp = evaluate_js(page, "window.__slice.csp")
        page.screenshot(path=str(scene.root / f"keyboard-{variant}.png"))
    passed = after == (chosen, before[1] + 1)
    scene.report[variant]["keyboard_reschedule"] = {
        "criterion": (
            "keyboard-only reschedule; focus returns to the moved appointment"
        ),
        "pass": passed,
        "raw": {
            "keys": keys,
            "key_count": len(keys),
            "before": before,
            "after": after,
            "chosen": chosen,
            "csp_violations": csp,
        },
    }
    assert passed, (before, after, chosen)
    assert csp == 0


# --------------------------------------------------------------------------
# 4. axe (zero violations of any impact) and 44px targets
# --------------------------------------------------------------------------


def _axe(page: Page, base_url: str, root: Path, name: str) -> list[dict[str, Any]]:
    # add_script_tag resolves once the script ran; inject axe once per page.
    if not evaluate_js(page, "typeof axe !== 'undefined'"):
        page.add_script_tag(url=f"{base_url}{AXE_URL}")
    violations: list[dict[str, Any]] = evaluate_js(page, AXE_RUN_JS)
    (root / f"axe-{name}.json").write_text(json.dumps(violations, indent=2))
    return violations


@pytest.mark.parametrize("variant", _variants())
def test_axe_zero_and_44px_targets_at_three_widths(variant: str, scene: Scene) -> None:
    seeded = scene.world.seed_day()
    browser = scene.browser
    scenes: dict[str, Any] = {}
    for width in (1280, 375, 320):
        with browser.new_context(
            locale="pt-BR",
            storage_state=scene.state,
            viewport={"width": width, "height": 900},
        ) as context:
            page = context.new_page()
            _open(
                page, scene.base_url, _path(scene.world, variant, seeded.day), variant
            )
            grid = _axe(page, scene.base_url, scene.root, f"{variant}-{width}-grid")
            small_grid = evaluate_js(page, TARGETS_JS)
            overflow = not evaluate_js(
                page, "document.documentElement.scrollWidth <= innerWidth"
            )
            page.screenshot(path=str(scene.root / f"grid-{variant}-{width}.png"))
            _slot_link(page, seeded.by_physician[scene.world.physicians[0]][0]).click()
            expect(page.locator("[data-move-dialog]")).to_have_attribute("open", "")
            dialog = _axe(page, scene.base_url, scene.root, f"{variant}-{width}-dialog")
            small_dialog = evaluate_js(page, TARGETS_JS)
            page.screenshot(path=str(scene.root / f"dialog-{variant}-{width}.png"))
        scenes[str(width)] = {
            "grid_violations": grid,
            "dialog_violations": dialog,
            "small_targets": small_grid + small_dialog,
            "page_overflows": overflow,
        }
    passed = all(
        not scene["grid_violations"]
        and not scene["dialog_violations"]
        and not scene["small_targets"]
        and not scene["page_overflows"]
        for scene in scenes.values()
    )
    scene.report[variant]["axe"] = {
        "criterion": "axe 0 violations (any impact), 44px targets, no page overflow",
        "pass": passed,
        "raw": scenes,
    }
    assert passed, scenes


# --------------------------------------------------------------------------
# 5. Two contexts, one appointment: conflict text, no lost update, video
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _variants())
def test_two_contexts_moving_one_appointment_lose_no_update(
    variant: str, scene: Scene
) -> None:
    seeded = scene.world.seed_day()
    physician = scene.world.physicians[0]
    appointment = seeded.by_physician[physician][0]
    first_slot, second_slot = seeded.free[physician][0], seeded.free[physician][1]
    browser = scene.browser
    videos = scene.root / f"video-{variant}"
    videos.mkdir(mode=0o700)
    size: ViewportSize = {"width": 1280, "height": 720}
    contexts: list[BrowserContext] = []
    try:
        for _ in range(2):
            context = browser.new_context(
                locale="pt-BR",
                storage_state=scene.state,
                viewport=size,
                record_video_dir=str(videos),
                record_video_size=size,
            )
            context.add_init_script(CSP_ONLY_JS)
            contexts.append(context)
        winner, loser = (context.new_page() for context in contexts)
        for page in (winner, loser):
            _open(
                page, scene.base_url, _path(scene.world, variant, seeded.day), variant
            )
            _connected(page)
        before = scene.world.stored(appointment)
        # The loser opens the dialog first: it holds the revision it rendered.
        _slot_link(loser, appointment).click()
        expect(loser.locator("[data-move-dialog]")).to_have_attribute("open", "")
        loser.locator("[data-move-start]").select_option(second_slot)
        # The winner moves the same appointment; the loser's grid refetches.
        with (
            loser.expect_response(_is_refetch(variant, seeded.day)) as refetched,
            winner.expect_response(_is_move(variant)) as won,
        ):
            _slot_link(winner, appointment).click()
            winner.locator("[data-move-start]").select_option(first_slot)
            winner.locator("[data-move-confirm]").click()
        assert won.value.status == 200
        assert refetched.value.status == 200
        expect(
            loser.locator(f'[data-appointment="{appointment}"]').first
        ).to_have_attribute("data-start", first_slot)
        with loser.expect_response(_is_move(variant)) as lost:
            loser.locator("[data-move-confirm]").click()
        conflict_text = gettext(
            "Someone else changed this appointment first. The grid shows their "
            "change; yours was not saved."
        )
        expect(_notice(loser)).to_contain_text(conflict_text)
        after = scene.world.stored(appointment)
        conflict_axe = _axe(loser, scene.base_url, scene.root, f"{variant}-conflict")
        csp = [evaluate_js(page, "window.__slice.csp") for page in (winner, loser)]
        loser.screenshot(path=str(scene.root / f"conflict-{variant}.png"))
        loser_video = loser.video
        winner_video = winner.video
    finally:
        for context in contexts:
            context.close()
    assert loser_video is not None
    assert winner_video is not None
    loser_video.save_as(str(scene.root / f"concurrency-{variant}.webm"))
    winner_video.save_as(str(scene.root / f"concurrency-{variant}-winner.webm"))
    passed = (
        lost.value.status == 409
        and after == (first_slot, before[1] + 1)
        and conflict_axe == []
    )
    scene.report[variant]["no_lost_update"] = {
        "criterion": "two contexts move one appointment: 409 conflict, winner kept",
        "pass": passed,
        "raw": {
            "before": before,
            "winner_target": first_slot,
            "loser_target": second_slot,
            "after": after,
            "loser_status": lost.value.status,
            "conflict_text": conflict_text,
            "conflict_axe_violations": conflict_axe,
            "csp_violations": csp,
            "video": f"concurrency-{variant}.webm",
        },
    }
    assert passed, scene.report[variant]["no_lost_update"]
    assert csp == [0, 0]


# --------------------------------------------------------------------------
# 6. JavaScript off: the grid is a native ledger of reschedule links
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _variants())
def test_js_off_grid_keeps_the_native_path(variant: str, scene: Scene) -> None:
    seeded = scene.world.seed_day()
    appointment = seeded.by_physician[scene.world.physicians[0]][0]
    browser = scene.browser
    with browser.new_context(
        locale="pt-BR", storage_state=scene.state, java_script_enabled=False
    ) as context:
        page = context.new_page()
        response = page.goto(scene.base_url + _path(scene.world, variant, seeded.day))
        assert response is not None
        assert response.status == 200
        rendered = page.locator("[data-appointment]").count()
        link = _slot_link(page, appointment)
        href = link.get_attribute("href")
        with page.expect_navigation():
            link.click()
        reached = urlsplit(page.url).path
        page.screenshot(path=str(scene.root / f"js-off-{variant}.png"))
    passed = (
        rendered >= 40
        and reached == f"/scheduling/appointments/{appointment}/reschedule/"
    )
    scene.report[variant]["js_off"] = {
        "criterion": "JS off: server grid; appointments link to their reschedule page",
        "pass": passed,
        "raw": {"appointments_rendered": rendered, "href": href, "reached": reached},
    }
    assert passed, scene.report[variant]["js_off"]
