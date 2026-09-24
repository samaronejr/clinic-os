"""Real-browser agenda and booking: the receptionist's day, served as ``clinic_app``.

Owner access is confined to synthetic staff setup, promised periods and the
extra rows that fill a page. Every wait subscribes to a navigation, a
response, a held request or a DOM state, never a timer. Captures contain
synthetic names only.

Evidence grid: the two journey tests run once per matrix width (1280, 768 and
375) and name every capture ``<state>-<width>``, so each state (today, empty,
booking, booking loading, booked, rows with long content, week, focused,
reschedule, reschedule loading, rescheduled, cancel, cancel loading,
cancelled, cancelled row, paginated, conflict, invalid, stale, physician,
denied) is rendered and checked at all three widths and no capture overwrites
another. The native test proves the flow without JavaScript, and the reflow
test adds 640 and 320 plus the preference scenes: forced colors, reduced
motion and a 1280px window at 200% browser zoom. Journey contexts run in the
Asia/Tokyo browser zone so every displayed hour is proven clinic-local.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import secrets
import threading
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.formats import date_format
from django.utils.translation import gettext, ngettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._protected import encrypt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route

CLINIC_A = "Clínica Vila Mariana"
CLINIC_B = "Clínica Aurora Sintética"  # sorts first, so selecting A is a real choice
CLINIC_ZONE = "America/Sao_Paulo"
BROWSER_ZONE = "Asia/Tokyo"
LONG_PHYSICIAN = (
    "dr-maximiliano-de-albuquerque-wanderley-sintetico-da-silva-neto-junior"
)
LONG_PATIENT = (
    "Maria Aparecida Conceição dos Santos Oliveira Ferreira de Albuquerque Sintética"
)
UNBROKEN_PATIENT = "Wolfeschlegelsteinhausenbergerdorff Sintética"
WIDTHS = (1280, 768, 640, 375, 320)
MATRIX_WIDTHS = (1280, 768, 375)
# Each scene works on its own civil day (a Tuesday) so no scene overlaps
# another's rows; the whole grid shifts by a run-unique number of weeks so a
# repeated run against one database never meets its own earlier rows.
RUN_SHIFT = timedelta(weeks=secrets.randbelow(150))
RUN_TAG = secrets.token_hex(2)  # registered names stay unique across runs


def _tuesday(weeks: int) -> str:
    return (date(2031, 6, 3) + RUN_SHIFT + timedelta(weeks=weeks)).isoformat()


DAYS = {1280: _tuesday(0), 768: _tuesday(1), 375: _tuesday(2)}
FAILURE_DAYS = {1280: _tuesday(10), 768: _tuesday(11), 375: _tuesday(12)}
NATIVE_DAY = _tuesday(20)
REFLOW_DAY = _tuesday(30)
PAGE_SIZE = 25
ZOOM_WINDOW = 1280  # physical window width behind the 200% zoom scene
ZOOM_FACTOR = 2
OK = 200
NO_CONTENT = 204
SEE_OTHER = 303
NOT_FOUND = 404
MIN_TARGET_PX = 44
NAVY = "rgb(15, 45, 58)"
PRIMARY = "rgb(0, 122, 135)"
SUNKEN = "rgb(238, 235, 228)"  # button-disabled background and a muted row
SELECTION = "rgb(222, 245, 243)"
DASH = "\u2013"  # the en dash between a start and an end
BOOKING_FORM = "#scheduling-booking form"
BOOKING_SUBMIT = f"{BOOKING_FORM} button[type=submit]"
BOOKING_PROGRESS = "#booking-progress"
BOOKING_ALERT = "#booking-errors"
TRANSITION_FORM = "#scheduling-transition form"
TRANSITION_SUBMIT = f"{TRANSITION_FORM} button[type=submit]"
TRANSITION_PROGRESS = "#transition-progress"
TRANSITION_ALERT = "#transition-errors"
STATUS = "#agenda-status"
ROWS = ".agenda-table .agenda-row"
SETTLED_JS = (
    "!document.querySelector('.htmx-request, .htmx-settling, .htmx-added,"
    ' [aria-busy="true"]\')'
)
BUSY_JS = """
async ([form, button, progress]) => {
  const f = document.querySelector(form);
  const b = document.querySelector(button);
  const p = document.querySelector(progress);
  // The disabled look transitions for --duration-1; record it settled.
  await Promise.all(b.getAnimations().map((animation) => animation.finished));
  const fs = getComputedStyle(f);
  const bs = getComputedStyle(b);
  const ps = getComputedStyle(p);
  return {
    aria_busy: f.getAttribute('aria-busy'),
    form_cursor: fs.cursor,
    button_disabled: b.disabled,
    button_cursor: bs.cursor,
    button_background: bs.backgroundColor,
    progress_text: p.textContent.trim(),
    progress_display: ps.display,
    progress_visibility: ps.visibility,
    progress_opacity: ps.opacity,
  };
}
"""


@pytest.fixture(scope="session")
def agenda_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed a two-clinic receptionist, physicians (one with TOTP), clinics B and C."""
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    suffix = uuid4().hex[:4]
    values = {
        "dsn": os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"],
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "clinic_b": str(uuid4()),
        "clinic_c": str(uuid4()),
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "receptionist": f"recepcao-{suffix}",
        "receptionist_id": str(uuid4()),
        "physician_a": f"dra-ana-sintetica-{suffix}",
        "physician_a_id": str(uuid4()),
        "physician_b": f"dr-bruno-sintetico-{suffix}",
        "physician_b_id": str(uuid4()),
        "physician_long": f"{LONG_PHYSICIAN}-{suffix}",
        "physician_long_id": str(uuid4()),
        "physician_c": f"dr-carlos-sintetico-{suffix}",
        "physician_c_id": str(uuid4()),
        "password": secrets.token_urlsafe(24),
        "totp_key": secrets.token_hex(20),
    }
    people = (
        (values["receptionist_id"], values["receptionist"]),
        (values["physician_a_id"], values["physician_a"]),
        (values["physician_b_id"], values["physician_b"]),
        (values["physician_long_id"], values["physician_long"]),
        (values["physician_c_id"], values["physician_c"]),
    )
    roles = (
        (values["receptionist_id"], values["clinic_a"], "receptionist"),
        (values["receptionist_id"], values["clinic_b"], "receptionist"),
        (values["physician_a_id"], values["clinic_a"], "physician"),
        (values["physician_b_id"], values["clinic_a"], "physician"),
        (values["physician_long_id"], values["clinic_a"], "physician"),
        (values["physician_c_id"], values["clinic_c"], "physician"),
    )
    with psycopg.connect(values["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [values["organization"]],
        )
        connection.execute(
            "UPDATE clinic_app.identity_clinic SET name = %s WHERE id = %s",
            [CLINIC_A, values["clinic_a"]],
        )
        for clinic_id, name in (
            (values["clinic_b"], CLINIC_B),
            (values["clinic_c"], "Clínica Sintética Sem Acesso"),
        ):
            connection.execute(
                "INSERT INTO clinic_app.identity_clinic "
                "(id, organization_id, name, crm_uf, timezone) "
                "VALUES (%s, %s, %s, 'SP', %s)",
                [clinic_id, values["organization"], name, CLINIC_ZONE],
            )
        for user_id, username in people:
            connection.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [
                    user_id,
                    username,
                    make_password(values["password"]),
                    f"{username}@agenda.invalid",
                ],
            )
        for user_id, clinic_id, role in roles:
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                [str(uuid4()), user_id, values["organization"], clinic_id, role],
            )
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [values["physician_a_id"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
            "user_id, throttling_failure_count, created_at) "
            "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
            "%s, 0, now())",
            [values["totp_key"], values["physician_a_id"]],
        )
    return values


@pytest.fixture
def agenda_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


@pytest.fixture(params=MATRIX_WIDTHS, ids=[f"{width}px" for width in MATRIX_WIDTHS])
def journey(request: pytest.FixtureRequest, agenda_browser: Browser) -> Iterator[Page]:
    """One pt-BR context per matrix width, in a browser zone far from the clinic."""
    context = agenda_browser.new_context(
        locale="pt-BR",
        timezone_id=BROWSER_ZONE,
        viewport={"width": int(request.param), "height": 900},
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    yield page
    context.close()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _width(page: Page) -> int:
    size = page.viewport_size
    assert size is not None
    return size["width"]


def _watch_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    return errors


def _capture(page: Page, root: Path, name: str, *, full_page: bool = True) -> str:
    destination = root / "agenda" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=full_page)
    destination.chmod(0o600)
    return destination.name


def _agenda_path(staff: dict[str, str], view: str, day: str, page: int = 1) -> str:
    return f"/scheduling/clinics/{staff['clinic_a']}/agenda/{view}/{day}/{page}/"


def _patients_path(staff: dict[str, str], clinic: str = "clinic_a") -> str:
    return f"/intake/clinics/{staff[clinic]}/patients/"


def _book_path(staff: dict[str, str], clinic: str = "clinic_a") -> str:
    return f"/scheduling/clinics/{staff[clinic]}/appointments/new/"


def _sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(f"{base_url}/auth/login/")
    page.locator("#id_username").fill(username)
    page.locator("#id_password").fill(password)
    with page.expect_navigation():
        page.locator("button[type=submit]").click()


def _sign_in_receptionist(page: Page, base_url: str, staff: dict[str, str]) -> None:
    _sign_in(page, base_url, staff["receptionist"], staff["password"])
    page.wait_for_url("**/auth/protected/")


def _sign_in_physician(page: Page, base_url: str, staff: dict[str, str]) -> None:
    # Independent scenes get fresh authenticator fixtures, not timing waits.
    staff["totp_key"] = secrets.token_hex(20)
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [staff["physician_a_id"]],
        )
        connection.execute(
            "UPDATE clinic_app.otp_totp_totpdevice SET key = %s, last_t = -1 "
            "WHERE user_id = %s",
            [staff["totp_key"], staff["physician_a_id"]],
        )
    _sign_in(page, base_url, staff["physician_a"], staff["password"])
    page.wait_for_url("**/auth/verify/**")
    token = TOTP(bytes.fromhex(staff["totp_key"]), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    with page.expect_navigation():
        page.locator("button[type=submit]").click()
    page.wait_for_url("**/auth/protected/")


def _utc(day: str, hhmm: str) -> datetime:
    """São Paulo civil minute to UTC; every journey day sits outside any DST."""
    local = datetime.fromisoformat(f"{day}T{hhmm}:00")
    return (local + timedelta(hours=3)).replace(tzinfo=UTC)


def _promise(
    staff: dict[str, str],
    physician_id: str,
    window: tuple[str, str, str],
    *,
    clinic: str = "clinic_a",
) -> None:
    """Insert one active period (day, start, end) through the owner fixture DSN."""
    day, start, end = window
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock (id, organization_id,"
            " clinic_id, practitioner_id, start_at, end_at, idempotency_key,"
            " create_fingerprint, created_at, updated_at, retired_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), NULL)",
            [
                str(uuid4()),
                staff["organization"],
                staff[clinic],
                physician_id,
                _utc(day, start),
                _utc(day, end),
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )


def _enrol(
    connection: psycopg.Connection[Any], staff: dict[str, str], name: str, clinic: str
) -> str:
    patient_id = str(uuid4())
    enrollment_id = str(uuid4())
    connection.execute(
        "INSERT INTO clinic_app.intake_patient "
        "(id, organization_id, full_name, birth_date, created_at) "
        "VALUES (%s, %s, %s, %s, now())",
        [
            patient_id,
            staff["organization"],
            encrypt(connection, "intake.patient.full_name", name.encode()),
            encrypt(connection, "intake.patient.birth_date", b"1990-01-01"),
        ],
    )
    connection.execute(
        "INSERT INTO clinic_app.intake_patientclinicenrollment "
        "(id, organization_id, clinic_id, patient_id, idempotency_key, "
        "create_fingerprint, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now())",
        [
            enrollment_id,
            staff["organization"],
            staff[clinic],
            patient_id,
            str(uuid4()),
            secrets.token_bytes(32),
        ],
    )
    return enrollment_id


def _seed_rows(
    staff: dict[str, str],
    rows: list[tuple[str, str, str, str, str]],
    *,
    clinic: str = "clinic_a",
) -> list[str]:
    """Insert scheduled appointments (name, physician_id, day, start, end)."""
    ids: list[str] = []
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        for name, physician_id, day, start, end in rows:
            enrollment_id = _enrol(connection, staff, name, clinic)
            patient_id = connection.execute(
                "SELECT patient_id FROM clinic_app.intake_patientclinicenrollment "
                "WHERE id = %s",
                [enrollment_id],
            ).fetchone()
            assert patient_id is not None
            appointment_id = str(uuid4())
            connection.execute(
                "INSERT INTO clinic_app.scheduling_appointment "
                "(id, organization_id, clinic_id, patient_id, practitioner_id, "
                "start_at, end_at, idempotency_key, create_fingerprint, status, "
                "cancellation_reason, cancelled_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'scheduled', NULL, "
                "NULL, now(), now())",
                [
                    appointment_id,
                    staff["organization"],
                    staff[clinic],
                    patient_id[0],
                    physician_id,
                    _utc(day, start),
                    _utc(day, end),
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
            ids.append(appointment_id)
    return ids


def _seed_enrollment(staff: dict[str, str], name: str, clinic: str = "clinic_a") -> str:
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        return _enrol(connection, staff, name, clinic)


def _stored(staff: dict[str, str], day: str) -> list[tuple[datetime, datetime, str]]:
    """Return (start_at, end_at, status) of clinic A rows starting on ``day``."""
    start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        rows = connection.execute(
            "SELECT start_at, end_at, status FROM clinic_app.scheduling_appointment "
            "WHERE clinic_id = %s AND start_at >= %s "
            "AND start_at < %s + interval '1 day' ORDER BY start_at, status",
            [staff["clinic_a"], start, start],
        ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _submit_expecting_error(page: Page, submit: str) -> None:
    """Submit over HTMX and wait for the swapped panel to settle."""
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as received:
        page.locator(submit).click()
    assert received.value.status == OK, received.value.status
    page.wait_for_function(SETTLED_JS)


@contextlib.contextmanager
def _observed_in_flight(
    page: Page,
    path: str,
    selectors: tuple[str, str, str],
    capture_name: str,
    root: Path,
) -> Iterator[dict[str, object]]:
    """Hold the next POST to ``path`` while its busy state is recorded and captured.

    The route handler runs while the request is paused, so what it records is
    the in-flight state by construction; the handler releases the request in
    its ``finally`` and the caller asserts on the record once the response or
    navigation it subscribed to has arrived.
    """
    observed: dict[str, object] = {}

    def hold(route: Route) -> None:
        if route.request.method != "POST" or observed:
            route.continue_()
            return
        try:
            observed.update(page.evaluate(BUSY_JS, list(selectors)))
            observed["capture"] = _capture(page, root, capture_name)
        finally:
            route.continue_()

    page.route(f"**{path}", hold)
    try:
        yield observed
    finally:
        page.unroute(f"**{path}", hold)


def _assert_busy(busy: dict[str, object], label: str) -> None:
    """The recorded in-flight state: ``aria-busy``, a disabled action and words."""
    assert busy["aria_busy"] == "true", busy
    assert busy["form_cursor"] == "progress", busy
    assert busy["button_disabled"] is True, busy
    assert busy["button_cursor"] == "not-allowed", busy
    assert busy["button_background"] == SUNKEN, busy
    assert busy["progress_text"] == label, busy
    # The indicator is a flex item, so its `inline-flex` computes to `flex`.
    assert busy["progress_display"] != "none", busy
    assert busy["progress_visibility"] == "visible", busy
    assert busy["progress_opacity"] == "1", busy


def _focused(page: Page) -> str:
    return str(
        page.evaluate(
            "document.activeElement && (document.activeElement.id"
            " || document.activeElement.className || document.activeElement.tagName)"
        )
    )


def _ring(page: Page) -> dict[str, str]:
    return dict(
        page.evaluate(
            "(() => { const s = getComputedStyle(document.activeElement);"
            " return {style: s.outlineStyle, width: s.outlineWidth,"
            " color: s.outlineColor}; })()"
        )
    )


def _no_overflow(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))


def _csrf(page: Page) -> str:
    value = page.locator("input[name=csrfmiddlewaretoken]").first.get_attribute("value")
    assert value
    return value


def _cookie(page: Page, name: str) -> str:
    return next(
        cookie.get("value", "")
        for cookie in page.context.cookies()
        if cookie.get("name") == name
    )


def _day_label(day: str) -> str:
    civil = date.fromisoformat(day)
    weekday = str(date_format(civil, "l"))
    return (
        f"{weekday[:1].upper()}{weekday[1:]}, {date_format(civil, 'SHORT_DATE_FORMAT')}"
    )


def _journey_name(page: Page) -> str:
    return f"Helena Sintética Agenda {_width(page)} {RUN_TAG}"


def _failure_names(page: Page) -> tuple[str, str]:
    width = _width(page)
    return (
        f"Paciente Sintético Primeiro {width} {RUN_TAG}",
        f"Paciente Sintético Segundo {width} {RUN_TAG}",
    )


def _range(start: str, end: str) -> str:
    return f"{start} {DASH} {end}"


def _short_date(day: str) -> str:
    return str(date_format(date.fromisoformat(day), "SHORT_DATE_FORMAT"))


def _day_status(total: int) -> str:
    return ngettext(
        "%(total)s appointment on this day.",
        "%(total)s appointments on this day.",
        total,
    ) % {"total": total}


def _week_status(total: int) -> str:
    return ngettext(
        "%(total)s appointment in this week.",
        "%(total)s appointments in this week.",
        total,
    ) % {"total": total}


def _rows(page: Page) -> list[dict[str, str]]:
    """Every agenda row as (time, patient, physician, status) in reading order."""
    rows: list[dict[str, str]] = page.locator(ROWS).evaluate_all(
        """rows => rows.map(row => ({
          time: row.querySelector('.agenda-time').textContent
            .replace(/\\s+/g, ' ').trim(),
          patient: row.querySelector('.agenda-patient').textContent.trim(),
          physician: row.querySelector('.agenda-physician').textContent.trim(),
          status: row.querySelector('.agenda-state').textContent.trim(),
          muted: row.classList.contains('table-row--muted') ? 'muted' : '',
          actions: Array.from(row.querySelectorAll('.agenda-actions a'))
            .map(a => a.getAttribute('href')).join(' '),
        }))"""
    )
    return rows


def _row_of(page: Page, patient: str) -> Locator:
    return page.locator(ROWS).filter(has_text=patient).first


def _assert_autofocus_target(page: Page, element_id: str) -> None:
    """The one autofocus candidate is the named element, focusable by script."""
    candidates = page.locator("[autofocus]")
    expect(candidates).to_have_count(1)
    expect(candidates).to_have_id(element_id)
    expect(candidates).to_have_attribute("tabindex", "-1")


def _register_patient(
    page: Page, base_url: str, staff: dict[str, str], name: str
) -> None:
    """Register ``name`` through the intake screens and land back on the search."""
    patients = _patients_path(staff)
    page.goto(f"{base_url}{patients}")
    page.locator("#id_q").fill(name)
    _submit_expecting_error(page, "#patient-search-form button[type=submit]")
    expect(page.locator(".intake-empty")).to_be_visible()
    with page.expect_navigation():
        page.locator(".intake-empty a.button--secondary").click()
    expect(page.locator("h1")).to_have_text(gettext("Register a patient"))
    page.locator("#id_full_name").fill(name)
    page.locator("#id_birth_date").fill("1990-05-17")
    with page.expect_navigation():
        page.locator("#patient-create-panel button[type=submit]").click()
    page.wait_for_url(f"**{patients}")
    expect(page.locator("#intake-registered")).to_contain_text(
        gettext("Patient registered")
    )


def _open_booking(page: Page, base_url: str, staff: dict[str, str], name: str) -> None:
    """Find ``name`` in the registry and open the booking screen for that row."""
    page.goto(f"{base_url}{_patients_path(staff)}")
    page.locator("#id_q").fill(name)
    _submit_expecting_error(page, "#patient-search-form button[type=submit]")
    with page.expect_navigation():
        page.locator(".intake-table tbody tr").filter(has_text=name).locator(
            "button", has_text=gettext("Book appointment")
        ).click()
    assert page.url == f"{base_url}{_book_path(staff)}"
    expect(page.locator("#booking-patient")).to_have_text(name)


def _fill_booking(page: Page, physician: str, start: str, end: str) -> None:
    page.locator("#id_practitioner").select_option(label=physician)
    page.locator("#id_start_local").fill(start)
    page.locator("#id_end_local").fill(end)


# --------------------------------------------------------------------------
# Happy journey steps
# --------------------------------------------------------------------------


def _select_clinic_and_open_today(
    page: Page, base_url: str, staff: dict[str, str], root: Path
) -> None:
    """From the workspace, pick clinic A in the switcher and read today's agenda."""
    width = _width(page)
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_B)
    page.locator(".nav-switch-summary").click()
    with page.expect_navigation():
        page.locator(".nav-switch-list a").filter(has_text=CLINIC_A).click()
    page.wait_for_url(f"**/scheduling/clinics/{staff['clinic_a']}/agenda/")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    expect(page.locator("h1")).to_have_text(gettext("Clinic agenda"))
    expect(page.locator(".eyebrow")).to_have_count(0)
    expect(page.locator(".lede")).to_contain_text(CLINIC_ZONE)
    expect(page.locator(".agenda-period .badge")).to_have_text(gettext("today"))
    expect(page.locator(".agenda-views a[aria-current=page]")).to_have_text(
        gettext("Day")
    )
    assert "?" not in page.url
    assert _no_overflow(page)
    _capture(page, root, f"today-{width}")
    del base_url


def _open_empty_day(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> None:
    page.goto(f"{base_url}{_agenda_path(staff, 'day', day)}")
    expect(page.locator(".agenda-period time")).to_have_text(_day_label(day))
    expect(page.locator(".agenda-period .badge")).to_have_count(0)
    expect(page.locator(STATUS)).to_have_text(_day_status(0))
    empty = page.locator("#agenda-empty")
    expect(empty).to_contain_text(
        gettext(
            "No appointment on this day. To book one, find the patient in the "
            "registry and press “Book appointment”."
        )
    )
    expect(empty.locator("a.button")).to_have_text(gettext("Find a patient to book"))
    expect(page.locator(".agenda-table")).to_have_count(0)
    assert _no_overflow(page)
    _capture(page, root, f"empty-{_width(page)}")


def _register_find_and_book(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> dict[str, object]:
    """Register, open booking from the empty state, book with the POST held."""
    width = _width(page)
    name = _journey_name(page)
    with page.expect_navigation():
        page.locator("#agenda-empty a.button").click()
    expect(page.locator("h1")).to_have_text(gettext("Patient search"))
    _register_patient(page, base_url, staff, name)
    _open_booking(page, base_url, staff, name)
    expect(page.locator("h1")).to_have_text(gettext("Book an appointment"))
    expect(page.locator(".eyebrow")).to_have_count(0)
    expect(page.locator(".lede")).to_contain_text(CLINIC_ZONE)
    windows = page.locator(".booking-windows tbody tr")
    window = windows.filter(has_text=staff["physician_a"]).filter(
        has_text=_short_date(day)
    )
    expect(window).to_have_count(1)
    expect(window).to_contain_text(_range("08:00", "12:00"))
    expect(page.locator(".booking-windows time").first).to_have_attribute(
        "datetime", re.compile(r"^\d{4}-\d{2}-\d{2}$")
    )
    expect(page.locator("#booking-windows-status")).to_have_attribute("role", "status")
    assert _no_overflow(page)
    _capture(page, root, f"booking-{width}")
    _fill_booking(page, staff["physician_a"], f"{day}T09:00", f"{day}T09:30")
    with (
        _observed_in_flight(
            page,
            _book_path(staff),
            (BOOKING_FORM, BOOKING_SUBMIT, BOOKING_PROGRESS),
            f"booking-loading-{width}",
            root,
        ) as busy,
        page.expect_navigation(),
    ):
        page.locator(BOOKING_SUBMIT).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    _assert_busy(busy, gettext("Booking…"))
    notice = page.locator("#appointment-booked")
    expect(notice).to_have_attribute("role", "status")
    expect(notice).to_contain_text(gettext("Appointment booked"))
    expect(page.locator(STATUS)).to_have_text(_day_status(1))
    rows = _rows(page)
    assert len(rows) == 1, rows
    assert rows[0]["time"] == _range("09:00", "09:30"), rows
    assert rows[0]["patient"] == name, rows
    assert rows[0]["physician"].endswith(staff["physician_a"]), rows
    assert rows[0]["status"] == gettext("Scheduled"), rows
    assert rows[0]["muted"] == "", rows
    assert "?" not in page.url
    assert name not in page.url
    # Stored as UTC (São Paulo -03:00) while the browser sits in Asia/Tokyo.
    assert _stored(staff, day) == [
        (_utc(day, "09:00"), _utc(day, "09:30"), "scheduled")
    ]
    assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == (
        BROWSER_ZONE
    )
    assert _no_overflow(page)
    _capture(page, root, f"booked-{width}")
    # Reload: the notice is consumed once and the row stays.
    page.reload()
    expect(page.locator("#appointment-booked")).to_have_count(0)
    assert len(_rows(page)) == 1
    return busy


def _rows_with_long_content(
    page: Page, staff: dict[str, str], day: str, root: Path
) -> None:
    """More rows on the day: long names and a 70-character physician, in start order."""
    name = _journey_name(page)
    _seed_rows(
        staff,
        [
            (LONG_PATIENT, staff["physician_b_id"], day, "13:00", "13:30"),
            (UNBROKEN_PATIENT, staff["physician_long_id"], day, "08:00", "08:45"),
        ],
    )
    page.reload()
    expect(page.locator(STATUS)).to_have_text(_day_status(3))
    rows = _rows(page)
    assert [row["time"] for row in rows] == [
        _range("08:00", "08:45"),
        _range("09:00", "09:30"),
        _range("13:00", "13:30"),
    ], rows
    assert [row["patient"] for row in rows] == [UNBROKEN_PATIENT, name, LONG_PATIENT]
    assert rows[0]["physician"].endswith(staff["physician_long"])
    assert all(row["actions"].count("/reschedule/") == 1 for row in rows), rows
    assert all(row["actions"].count("/cancel/") == 1 for row in rows), rows
    assert _no_overflow(page)
    _capture(page, root, f"rows-{_width(page)}")


def _week_and_steps(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """Week view groups the rows under the day; the steps walk days and weeks."""
    width = _width(page)
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    with page.expect_navigation():
        page.locator(".agenda-views a").filter(has_text=gettext("Week")).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'week', day)}")
    monday = date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
    sunday = monday + timedelta(days=6)
    expect(page.locator(".agenda-period")).to_contain_text(
        _short_date(monday.isoformat())
    )
    expect(page.locator(".agenda-period")).to_contain_text(
        _short_date(sunday.isoformat())
    )
    expect(page.locator(".agenda-views a[aria-current=page]")).to_have_text(
        gettext("Week")
    )
    expect(page.locator(STATUS)).to_have_text(_week_status(3))
    headers = page.locator(".agenda-day-row th")
    expect(headers).to_have_count(1)
    expect(headers.first).to_have_text(_day_label(day))
    expect(headers.first.locator("time")).to_have_attribute("datetime", day)
    assert len(_rows(page)) == 3
    assert _no_overflow(page)
    _capture(page, root, f"week-{width}")
    with page.expect_navigation():
        page.locator(".agenda-steps a").filter(has_text=gettext("Next week")).click()
    expect(page.locator(STATUS)).to_have_text(_week_status(0))
    expect(page.locator("#agenda-empty")).to_contain_text(
        gettext(
            "No appointment in this week. To book one, find the patient in the "
            "registry and press “Book appointment”."
        )
    )
    with page.expect_navigation():
        page.locator(".agenda-steps a").filter(
            has_text=gettext("Previous week")
        ).click()
    assert len(_rows(page)) == 3
    with page.expect_navigation():
        page.locator(".agenda-views a").filter(has_text=gettext("Day")).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    with page.expect_navigation():
        page.locator(".agenda-steps a").filter(has_text=gettext("Next day")).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', next_day)}")
    expect(page.locator(".agenda-period time")).to_have_text(_day_label(next_day))
    with page.expect_navigation():
        page.locator(".agenda-steps a").filter(has_text=gettext("Previous day")).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    assert base_url in page.url


def _keyboard_reaches_the_row_action(
    page: Page, staff: dict[str, str], root: Path
) -> None:
    """Tab from the view switch into the ledger and onto its first row action."""
    page.locator(".agenda-views a").last.focus()
    page.keyboard.press("Tab")
    assert _focused(page) == "table-scroll"
    page.keyboard.press("Tab")
    label = str(page.evaluate("document.activeElement.textContent")).strip()
    assert label.startswith(gettext("Reschedule"))
    assert label.endswith(
        gettext("%(patient)s at %(start)s")
        % {"patient": UNBROKEN_PATIENT, "start": "08:00"}
    )
    ring = _ring(page)
    assert ring["style"] == "solid"
    assert ring["color"] == NAVY
    assert _no_overflow(page)
    _capture(page, root, f"focused-{_width(page)}")
    del staff


def _reschedule(
    page: Page, staff: dict[str, str], day: str, root: Path
) -> dict[str, object]:
    """Move the journey appointment with the POST held; land on the same object."""
    width = _width(page)
    name = _journey_name(page)
    with page.expect_navigation():
        _row_of(page, name).locator("a").filter(has_text=gettext("Reschedule")).click()
    assert page.url.endswith("/reschedule/")
    appointment_url = page.url
    expect(page.locator("h1")).to_have_text(gettext("Reschedule an appointment"))
    summary = page.locator(".transition-summary")
    expect(summary).to_contain_text(name)
    expect(summary).to_contain_text(f"{_short_date(day)} {_range('09:00', '09:30')}")
    expect(summary).to_contain_text(staff["physician_a"])
    expect(summary.locator(".badge")).to_have_text(gettext("Scheduled"))
    expect(page.locator("#id_practitioner")).to_have_count(0)
    assert _no_overflow(page)
    _capture(page, root, f"reschedule-{width}")
    page.locator("#id_start_local").fill(f"{day}T10:00")
    page.locator("#id_end_local").fill(f"{day}T10:30")
    with (
        _observed_in_flight(
            page,
            "*/reschedule/",
            (TRANSITION_FORM, TRANSITION_SUBMIT, TRANSITION_PROGRESS),
            f"reschedule-loading-{width}",
            root,
        ) as busy,
        page.expect_navigation(),
    ):
        page.locator(TRANSITION_SUBMIT).click()
    page.wait_for_url(appointment_url)
    _assert_busy(busy, gettext("Saving…"))
    notice = page.locator("#appointment-rescheduled")
    expect(notice).to_have_attribute("role", "status")
    expect(notice).to_contain_text(gettext("Appointment rescheduled"))
    expect(summary).to_contain_text(f"{_short_date(day)} {_range('10:00', '10:30')}")
    expect(summary.locator("time").first).to_have_attribute("datetime", f"{day}T10:00")
    assert _no_overflow(page)
    _capture(page, root, f"rescheduled-{width}")
    # Reload: the notice is consumed once; nothing is replayed.
    page.reload()
    expect(page.locator("#appointment-rescheduled")).to_have_count(0)
    expect(summary).to_contain_text(f"{_short_date(day)} {_range('10:00', '10:30')}")
    # The return link opens the appointment's own day; history stays safe.
    with page.expect_navigation():
        page.locator("#scheduling-transition a.button").click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    row = _row_of(page, name)
    expect(row.locator(".agenda-time")).to_have_text(_range("10:00", "10:30"))
    page.go_back()
    page.wait_for_url(appointment_url)
    expect(summary).to_contain_text(f"{_short_date(day)} {_range('10:00', '10:30')}")
    expect(page.locator(TRANSITION_FORM)).to_have_count(1)
    page.go_forward()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    expect(_row_of(page, name).locator(".agenda-time")).to_have_text(
        _range("10:00", "10:30")
    )
    stored = _stored(staff, day)
    assert (_utc(day, "10:00"), _utc(day, "10:30"), "scheduled") in stored
    return busy


def _cancel(
    page: Page, staff: dict[str, str], day: str, root: Path
) -> dict[str, object]:
    """Cancel with a closed reason and the POST held; the state is terminal."""
    width = _width(page)
    name = _journey_name(page)
    with page.expect_navigation():
        _row_of(page, name).locator("a").filter(has_text=gettext("Cancel")).click()
    assert page.url.endswith("/cancel/")
    appointment_url = page.url
    expect(page.locator("h1")).to_have_text(gettext("Cancel an appointment"))
    expect(page.locator(".transition-summary")).to_contain_text(
        f"{_short_date(day)} {_range('10:00', '10:30')}"
    )
    expect(page.locator("#id_reason option")).to_have_count(5)
    expect(page.locator(TRANSITION_SUBMIT)).to_have_class(re.compile("button--danger"))
    assert _no_overflow(page)
    _capture(page, root, f"cancel-{width}")
    page.locator("#id_reason").select_option("patient_request")
    with (
        _observed_in_flight(
            page,
            "*/cancel/",
            (TRANSITION_FORM, TRANSITION_SUBMIT, TRANSITION_PROGRESS),
            f"cancel-loading-{width}",
            root,
        ) as busy,
        page.expect_navigation(),
    ):
        page.locator(TRANSITION_SUBMIT).click()
    page.wait_for_url(appointment_url)
    _assert_busy(busy, gettext("Saving…"))
    notice = page.locator("#appointment-cancelled")
    expect(notice).to_have_attribute("role", "status")
    expect(notice).to_contain_text(gettext("Appointment cancelled"))
    summary = page.locator(".transition-summary")
    expect(summary.locator(".badge")).to_have_text(gettext("Cancelled"))
    expect(summary).to_contain_text(gettext("Patient request"))
    expect(page.locator("form")).to_have_count(0)
    expect(page.locator("button")).to_have_count(0)
    expect(page.locator("#transition-status")).to_have_attribute("role", "status")
    assert _no_overflow(page)
    _capture(page, root, f"cancelled-{width}")
    # The same object's reschedule screen is terminal too: nothing to submit.
    page.goto(appointment_url.replace("/cancel/", "/reschedule/"))
    expect(page.locator("form")).to_have_count(0)
    expect(page.locator("#transition-status")).to_contain_text(
        gettext("This appointment is cancelled, so it can no longer be changed.")
    )
    with page.expect_navigation():
        page.locator("#scheduling-transition a.button").click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    row = _row_of(page, name)
    expect(row).to_have_class(re.compile("table-row--muted"))
    expect(row.locator(".badge")).to_have_text(gettext("Cancelled"))
    expect(row.locator(".agenda-actions a")).to_have_count(0)
    expect(row.locator(".agenda-no-action")).to_have_text(
        gettext("No action available")
    )
    background = row.evaluate("el => getComputedStyle(el).backgroundColor")
    assert background == SUNKEN
    expect(page.locator(STATUS)).to_have_text(_day_status(3))
    assert (_utc(day, "10:00"), _utc(day, "10:30"), "cancelled") in _stored(staff, day)
    assert _no_overflow(page)
    _capture(page, root, f"cancelled-row-{width}")
    return busy


def _paginated(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """A day past the page size splits into state-free pages."""
    width = _width(page)
    busy_day = (date.fromisoformat(day) + timedelta(days=2)).isoformat()
    _promise(staff, staff["physician_b_id"], (busy_day, "07:00", "19:00"))
    _seed_rows(
        staff,
        [
            (
                f"Paciente Sintético {index:02d}",
                staff["physician_b_id"],
                busy_day,
                f"{7 + index // 4:02d}:{(index % 4) * 15:02d}",
                f"{7 + index // 4:02d}:{(index % 4) * 15 + 10:02d}",
            )
            for index in range(PAGE_SIZE + 1)
        ],
    )
    page.goto(f"{base_url}{_agenda_path(staff, 'day', busy_day)}")
    expect(page.locator(STATUS)).to_have_text(
        f"{_day_status(PAGE_SIZE + 1)} "
        + gettext("Page %(page)s of %(pages)s.") % {"page": 1, "pages": 2}
    )
    assert len(_rows(page)) == PAGE_SIZE
    assert _no_overflow(page)
    _capture(page, root, f"paginated-{width}", full_page=False)
    with page.expect_navigation():
        page.locator(".agenda-pagination a").filter(
            has_text=gettext("Next page")
        ).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', busy_day, 2)}")
    assert "?" not in page.url
    assert len(_rows(page)) == 1
    expect(page.locator(".agenda-pagination a")).to_have_text(
        [gettext("Previous page")]
    )


def test_receptionist_books_locates_reschedules_and_cancels_in_portuguese(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    staff = agenda_staff
    day = DAYS[width]
    _promise(staff, staff["physician_a_id"], (day, "08:00", "12:00"))
    _promise(staff, staff["physician_b_id"], (day, "13:00", "17:00"))
    _promise(staff, staff["physician_long_id"], (day, "08:00", "09:00"))
    _sign_in_receptionist(page, renewal_base_url, staff)
    _select_clinic_and_open_today(page, renewal_base_url, staff, root)
    _open_empty_day(page, renewal_base_url, staff, day, root)
    booking = _register_find_and_book(page, renewal_base_url, staff, day, root)
    _rows_with_long_content(page, staff, day, root)
    _week_and_steps(page, renewal_base_url, staff, day, root)
    _keyboard_reaches_the_row_action(page, staff, root)
    rescheduling = _reschedule(page, staff, day, root)
    cancelling = _cancel(page, staff, day, root)
    _paginated(page, renewal_base_url, staff, day, root)
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "agenda",
            "width": width,
            "assertion": "clinic selected in the switcher, today's agenda with the"
            " badge, empty day naming the next step, register and find a patient,"
            " book with the POST held (busy state), completion notice and row in"
            " place, clinic-local hours stored as UTC while the browser sits in"
            " Asia/Tokyo, rows in start order with long names, week view grouped"
            " by day, day/week steps, keyboard focus order to the row action,"
            " reschedule with the POST held, same-object notice, safe reload and"
            " back/forward, cancel with the POST held, terminal state on both"
            " transition screens, muted row without actions, state-free pages",
            "browser_zone": BROWSER_ZONE,
            "clinic_zone": CLINIC_ZONE,
            "in_flight": {
                "book": booking,
                "reschedule": rescheduling,
                "cancel": cancelling,
            },
            "console_errors": errors,
        }
    )


def test_native_post_books_reschedules_and_cancels_without_javascript(
    agenda_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
) -> None:
    context: BrowserContext = agenda_browser.new_context(
        locale="pt-BR",
        timezone_id=BROWSER_ZONE,
        viewport={"width": 768, "height": 1024},
        java_script_enabled=False,
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    staff = agenda_staff
    root = renewal_artifact_root
    day = NATIVE_DAY
    name = f"Otto Sintético Nativo {RUN_TAG}"
    base = renewal_base_url
    try:
        _promise(staff, staff["physician_a_id"], (day, "08:00", "12:00"))
        _sign_in_receptionist(page, base, staff)
        _register_patient(page, base, staff, name)
        _open_booking(page, base, staff, name)
        _native_book(page, base, staff, day, root)
        _native_transitions(page, staff, day, root)
    finally:
        context.close()


def _native_book(
    page: Page, base: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """Native validation error keeps the inputs; the retry lands on the agenda."""
    _fill_booking(page, staff["physician_a"], f"{day}T09:30", f"{day}T09:00")
    with page.expect_navigation():
        page.locator(BOOKING_SUBMIT).click()
    _assert_autofocus_target(page, "booking-errors")
    expect(page.locator(BOOKING_ALERT)).to_contain_text(
        gettext(
            "Enter a future window that starts and ends on the same clinic-local date."
        )
    )
    expect(page.locator("#id_start_local")).to_have_value(f"{day}T09:30")
    expect(page.locator("#id_end_local")).to_have_value(f"{day}T09:00")
    expect(page.locator("#id_practitioner")).to_have_value(staff["physician_a_id"])
    _capture(page, root, "native-invalid-768", full_page=False)
    page.locator("#id_end_local").fill(f"{day}T10:00")
    with page.expect_navigation():
        page.locator(BOOKING_SUBMIT).click()
    assert page.url == f"{base}{_agenda_path(staff, 'day', day)}"
    expect(page.locator("#appointment-booked")).to_be_visible()
    rows = _rows(page)
    assert [row["time"] for row in rows] == [_range("09:30", "10:00")]
    assert _no_overflow(page)
    _capture(page, root, "native-booked-768", full_page=False)


def _native_transitions(
    page: Page, staff: dict[str, str], day: str, root: Path
) -> None:
    """Native reschedule and cancel: one POST each, redirect to the same object."""
    name = f"Otto Sintético Nativo {RUN_TAG}"
    with page.expect_navigation():
        _row_of(page, name).locator("a").filter(has_text=gettext("Reschedule")).click()
    appointment_url = page.url
    page.locator("#id_start_local").fill(f"{day}T11:00")
    page.locator("#id_end_local").fill(f"{day}T11:30")
    with page.expect_navigation():
        page.locator(TRANSITION_SUBMIT).click()
    assert page.url == appointment_url
    expect(page.locator("#appointment-rescheduled")).to_be_visible()
    expect(page.locator(".transition-summary")).to_contain_text(
        _range("11:00", "11:30")
    )
    _capture(page, root, "native-rescheduled-768", full_page=False)
    # Native cancel: the reason travels in the body; the state is terminal.
    page.goto(appointment_url.replace("/reschedule/", "/cancel/"))
    page.locator("#id_reason").select_option("clinic_request")
    with page.expect_navigation():
        page.locator(TRANSITION_SUBMIT).click()
    expect(page.locator("#appointment-cancelled")).to_be_visible()
    expect(page.locator("form")).to_have_count(0)
    _capture(page, root, "native-cancelled-768", full_page=False)
    with page.expect_navigation():
        page.locator("#scheduling-transition a.button").click()
    expect(_row_of(page, name)).to_have_class(re.compile("table-row--muted"))
    assert _stored(staff, day) == [
        (_utc(day, "11:00"), _utc(day, "11:30"), "cancelled")
    ]


# --------------------------------------------------------------------------
# Failure journey steps
# --------------------------------------------------------------------------


def _double_booking(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """A window another patient already holds with that physician is refused."""
    width = _width(page)
    second = _failure_names(page)[1]
    _open_booking(page, base_url, staff, second)
    _fill_booking(page, staff["physician_a"], f"{day}T09:00", f"{day}T09:30")
    _submit_expecting_error(page, BOOKING_SUBMIT)
    alert = page.locator(BOOKING_ALERT)
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text(gettext("We could not book that appointment"))
    expect(alert).to_contain_text(
        gettext("That window is no longer free for this physician or patient.")
    )
    assert _focused(page) == "booking-errors"
    expect(page.locator("#id_practitioner")).to_have_value(staff["physician_a_id"])
    expect(page.locator("#id_start_local")).to_have_value(f"{day}T09:00")
    expect(page.locator("#id_end_local")).to_have_value(f"{day}T09:30")
    expect(page.locator("#booking-patient")).to_have_text(second)
    assert page.url == f"{base_url}{_book_path(staff)}"
    assert _no_overflow(page)
    _capture(page, root, f"conflict-{width}")
    # The retry with a free window lands; the refused one was never saved.
    page.locator("#id_start_local").fill(f"{day}T09:30")
    page.locator("#id_end_local").fill(f"{day}T10:00")
    with page.expect_navigation():
        page.locator(BOOKING_SUBMIT).click()
    page.wait_for_url(f"**{_agenda_path(staff, 'day', day)}")
    assert [row["time"] for row in _rows(page)] == [
        _range("09:00", "09:30"),
        _range("09:30", "10:00"),
    ]


def _race_two_bookings(
    page: Page, base_url: str, staff: dict[str, str], day: str
) -> dict[str, object]:
    """Two concurrent bookings of one window: exactly one appointment exists."""
    third = _seed_enrollment(staff, "Paciente Sintético Corrida Um")
    fourth = _seed_enrollment(staff, "Paciente Sintético Corrida Dois")
    page.goto(f"{base_url}{_agenda_path(staff, 'day', day)}")
    session = _cookie(page, "sessionid")
    csrf = _cookie(page, "csrftoken")
    parts = urlsplit(base_url)
    host = parts.hostname
    port = parts.port
    assert host is not None
    assert port is not None
    barrier = threading.Barrier(2)
    statuses: dict[str, int] = {}

    def post(label: str, enrollment_id: str) -> None:
        body = urlencode(
            {
                "csrfmiddlewaretoken": csrf,
                "mode": "create",
                "enrollment_id": enrollment_id,
                "practitioner": staff["physician_a_id"],
                "start_local": f"{day}T10:00",
                "end_local": f"{day}T10:30",
                "idempotency_key": str(uuid4()),
            }
        )
        connection = http.client.HTTPConnection(host, port, timeout=30)
        try:
            barrier.wait(timeout=10)
            connection.request(
                "POST",
                _book_path(staff),
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": f"sessionid={session}; csrftoken={csrf}",
                    "Referer": f"{base_url}{_book_path(staff)}",
                    "Origin": base_url,
                },
            )
            statuses[label] = connection.getresponse().status
        finally:
            connection.close()

    workers = [
        threading.Thread(target=post, args=("one", third)),
        threading.Thread(target=post, args=("two", fourth)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert sorted(statuses.values()) == [OK, SEE_OTHER], statuses
    at_ten = [row for row in _stored(staff, day) if row[0] == _utc(day, "10:00")]
    assert at_ten == [(_utc(day, "10:00"), _utc(day, "10:30"), "scheduled")]
    page.reload()
    assert [row["time"] for row in _rows(page)].count(_range("10:00", "10:30")) == 1
    return {"statuses": statuses, "stored_at_10": len(at_ten)}


def _invalid_times(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """Reversed, outside-window and cross-day times are refused with the fix named."""
    width = _width(page)
    second = _failure_names(page)[1]
    _open_booking(page, base_url, staff, second)
    _fill_booking(page, staff["physician_a"], f"{day}T11:30", f"{day}T11:00")
    _submit_expecting_error(page, BOOKING_SUBMIT)
    expect(page.locator(BOOKING_ALERT)).to_contain_text(
        gettext(
            "Enter a future window that starts and ends on the same clinic-local date."
        )
    )
    expect(page.locator("#id_start_local")).to_have_value(f"{day}T11:30")
    assert _no_overflow(page)
    _capture(page, root, f"invalid-{width}")
    _fill_booking(page, staff["physician_a"], f"{day}T07:00", f"{day}T07:30")
    _submit_expecting_error(page, BOOKING_SUBMIT)
    expect(page.locator(BOOKING_ALERT)).to_contain_text(
        gettext(
            "Choose a window inside one of the physician's promised availability "
            "blocks."
        )
    )
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    _fill_booking(page, staff["physician_a"], f"{day}T11:45", f"{next_day}T00:15")
    _submit_expecting_error(page, BOOKING_SUBMIT)
    expect(page.locator(BOOKING_ALERT)).to_be_visible()
    assert [row[2] for row in _stored(staff, day)].count("scheduled") == 3


def _stale_reschedule_and_replayed_cancel(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> dict[str, object]:
    """A reschedule page left open after a cancel is refused; cancel replays no-op."""
    width = _width(page)
    first = _failure_names(page)[0]
    page.goto(f"{base_url}{_agenda_path(staff, 'day', day)}")
    with page.expect_navigation():
        _row_of(page, first).locator("a").filter(has_text=gettext("Reschedule")).click()
    reschedule_url = page.url
    cancel_url = reschedule_url.replace("/reschedule/", "/cancel/")
    # Meanwhile the appointment is cancelled elsewhere (a real POST of this session).
    cancelled = page.request.post(
        cancel_url,
        form={
            "csrfmiddlewaretoken": _cookie(page, "csrftoken"),
            "reason": "patient_request",
        },
        headers={"Referer": reschedule_url},
        max_redirects=0,
    )
    assert cancelled.status == SEE_OTHER
    page.locator("#id_start_local").fill(f"{day}T11:00")
    page.locator("#id_end_local").fill(f"{day}T11:30")
    _submit_expecting_error(page, TRANSITION_SUBMIT)
    alert = page.locator(TRANSITION_ALERT)
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text(
        gettext("This appointment is cancelled and can no longer be moved.")
    )
    assert _focused(page) == "transition-errors"
    expect(page.locator(".transition-summary .badge")).to_have_text(
        gettext("Cancelled")
    )
    expect(page.locator(TRANSITION_FORM)).to_have_count(0)
    assert _no_overflow(page)
    _capture(page, root, f"stale-{width}")
    page.reload()
    expect(page.locator("form")).to_have_count(0)
    expect(page.locator("#transition-status")).to_have_attribute("role", "status")
    # Replays: the same reason is a quiet no-op; another reason is refused.
    page.goto(f"{base_url}{_agenda_path(staff, 'day', day)}")
    same = page.request.post(
        cancel_url,
        form={
            "csrfmiddlewaretoken": _cookie(page, "csrftoken"),
            "reason": "patient_request",
        },
        headers={"Referer": cancel_url},
        max_redirects=0,
    )
    other = page.request.post(
        cancel_url,
        form={"csrfmiddlewaretoken": _cookie(page, "csrftoken"), "reason": "other"},
        headers={"Referer": cancel_url},
        max_redirects=0,
    )
    assert same.status == SEE_OTHER
    assert other.status == OK
    assert gettext(
        "This appointment was already cancelled for a different reason."
    ) in (other.text())
    stored = _stored(staff, day)
    assert [row[2] for row in stored if row[0] == _utc(day, "09:00")] == ["cancelled"]
    row = _row_of(page, first)
    expect(row).to_have_class(re.compile("table-row--muted"))
    # The freed window is booked again for another patient; the cancelled row stays.
    fifth = _seed_enrollment(staff, "Paciente Sintético Reaproveitado")
    rebooked = page.request.post(
        f"{base_url}{_book_path(staff)}",
        form={
            "csrfmiddlewaretoken": _cookie(page, "csrftoken"),
            "mode": "create",
            "enrollment_id": fifth,
            "practitioner": staff["physician_a_id"],
            "start_local": f"{day}T09:00",
            "end_local": f"{day}T09:30",
            "idempotency_key": str(uuid4()),
        },
        headers={"Referer": page.url},
        max_redirects=0,
    )
    assert rebooked.status == SEE_OTHER
    assert [row[2] for row in _stored(staff, day) if row[0] == _utc(day, "09:00")] == [
        "cancelled",
        "scheduled",
    ]
    page.reload()
    at_nine = [row for row in _rows(page) if row["time"] == _range("09:00", "09:30")]
    assert sorted(row["muted"] for row in at_nine) == ["", "muted"], at_nine
    return {"same_reason": same.status, "other_reason": other.status}


def _replayed_booking_key(
    page: Page, base_url: str, staff: dict[str, str], day: str
) -> None:
    """Replaying one booking submission (same key) never creates a second row."""
    second = _failure_names(page)[1]
    _open_booking(page, base_url, staff, second)
    key = page.locator("input[name=idempotency_key]").get_attribute("value")
    enrollment = page.locator("input[name=enrollment_id]").get_attribute("value")
    assert key
    assert enrollment
    form: dict[str, str | float | bool] = {
        "csrfmiddlewaretoken": _cookie(page, "csrftoken"),
        "mode": "create",
        "enrollment_id": enrollment,
        "practitioner": staff["physician_a_id"],
        "start_local": f"{day}T11:00",
        "end_local": f"{day}T11:30",
        "idempotency_key": key,
    }
    first = page.request.post(
        f"{base_url}{_book_path(staff)}",
        form=form,
        headers={"Referer": page.url},
        max_redirects=0,
    )
    replay = page.request.post(
        f"{base_url}{_book_path(staff)}",
        form=form,
        headers={"Referer": page.url},
        max_redirects=0,
    )
    assert [first.status, replay.status] == [SEE_OTHER, SEE_OTHER]
    at_eleven = [row for row in _stored(staff, day) if row[0] == _utc(day, "11:00")]
    assert at_eleven == [(_utc(day, "11:00"), _utc(day, "11:30"), "scheduled")]
    # The same key with other details is a conflict, not a second booking.
    changed = page.request.post(
        f"{base_url}{_book_path(staff)}",
        form={**form, "end_local": f"{day}T11:45"},
        headers={"Referer": page.url},
        max_redirects=0,
    )
    assert changed.status == OK
    assert (
        gettext("This booking was already submitted with different details.")
        in changed.text()
    )


def _physician_view(
    source: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> list[str]:
    """The physician reads only their own rows and holds no write control."""
    width = _width(source)
    browser = source.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        timezone_id=BROWSER_ZONE,
        viewport={"width": width, "height": 900},
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    errors = _watch_errors(page)
    try:
        _sign_in_physician(page, base_url, staff)
        page.goto(f"{base_url}{_agenda_path(staff, 'day', day)}")
        expect(page.locator("h1")).to_have_text(gettext("Clinic agenda"))
        expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
        # The only write controls are the physician's own encounter and
        # questionnaire actions; reception's scheduling controls never render.
        expect(page.locator(".agenda-actions")).to_have_count(0)
        actions = page.locator("form button[type=submit]").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )
        assert actions
        assert all(value in {"open", "appointment"} for value in actions)
        rows = _rows(page)
        assert rows, rows
        assert all(row["physician"].endswith(staff["physician_a"]) for row in rows)
        assert staff["physician_b"] not in page.content()
        assert "/reschedule/" not in page.content()
        assert "/cancel/" not in page.content()
        assert _no_overflow(page)
        _capture(page, root, f"physician-{width}")
        # Write attempts by the physician are refused, never offered.
        cancel_url = f"{base_url}/scheduling/appointments/{staff['first_id']}/cancel/"
        refused = page.request.post(
            cancel_url,
            form={"csrfmiddlewaretoken": _cookie(page, "csrftoken"), "reason": "other"},
            headers={"Referer": f"{base_url}{_agenda_path(staff, 'day', day)}"},
        )
        assert refused.status == NOT_FOUND
        opened = page.request.get(cancel_url)
        assert opened.status == NOT_FOUND
    finally:
        context.close()
    return errors


def _cross_clinic(
    page: Page, base_url: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """Clinic C (no role) is refused on agenda, booking and transitions, never named."""
    width = _width(page)
    agenda_c = f"/scheduling/clinics/{staff['clinic_c']}/agenda/"
    response = page.goto(f"{base_url}{agenda_c}")
    assert response is not None
    assert response.status == NOT_FOUND
    expect(page.locator("h1")).to_have_text(gettext("Page unavailable"))
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    assert "Sem Acesso" not in page.content()
    assert _no_overflow(page)
    _capture(page, root, f"denied-{width}")
    # A clinic C appointment and enrollment are indistinguishable from unknown ones.
    _promise(staff, staff["physician_c_id"], (day, "14:00", "17:00"), clinic="clinic_c")
    foreign_ids = _seed_rows(
        staff,
        [("Paciente Sintético Alheio", staff["physician_c_id"], day, "15:00", "15:30")],
        clinic="clinic_c",
    )
    foreign_enrollment = _seed_enrollment(
        staff, "Paciente Sintético Alheio 2", "clinic_c"
    )
    page.goto(f"{base_url}{_agenda_path(staff, 'day', day)}")
    csrf = _cookie(page, "csrftoken")
    referer = f"{base_url}{_agenda_path(staff, 'day', day)}"
    reschedule = page.request.get(
        f"{base_url}/scheduling/appointments/{foreign_ids[0]}/reschedule/"
    )
    assert reschedule.status == NOT_FOUND
    booked = page.request.post(
        f"{base_url}{_book_path(staff, 'clinic_c')}",
        form={
            "csrfmiddlewaretoken": csrf,
            "mode": "create",
            "enrollment_id": foreign_enrollment,
            "practitioner": staff["physician_c_id"],
            "start_local": f"{day}T15:30",
            "end_local": f"{day}T16:00",
            "idempotency_key": str(uuid4()),
        },
        headers={"Referer": referer},
    )
    assert booked.status == NOT_FOUND
    smuggled = page.request.post(
        f"{base_url}{_book_path(staff)}",
        form={
            "csrfmiddlewaretoken": csrf,
            "mode": "create",
            "enrollment_id": foreign_enrollment,
            "practitioner": staff["physician_a_id"],
            "start_local": f"{day}T11:30",
            "end_local": f"{day}T11:45",
            "idempotency_key": str(uuid4()),
        },
        headers={"Referer": referer},
    )
    assert smuggled.status == NOT_FOUND
    assert "Alheio" not in page.content()
    # Clinic B, where the receptionist also works, shows none of clinic A's rows.
    page.goto(f"{base_url}/scheduling/clinics/{staff['clinic_b']}/agenda/day/{day}/1/")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_B)
    expect(page.locator(STATUS)).to_have_text(_day_status(0))
    assert "Sintético" not in page.content()


def test_failures_show_recoverable_conflicts_and_keep_boundaries(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    staff = agenda_staff
    day = FAILURE_DAYS[width]
    first, second = _failure_names(page)
    _promise(staff, staff["physician_a_id"], (day, "08:00", "12:00"))
    staff["first_id"] = _seed_rows(
        staff, [(first, staff["physician_a_id"], day, "09:00", "09:30")]
    )[0]
    _seed_enrollment(staff, second)
    _sign_in_receptionist(page, renewal_base_url, staff)
    _double_booking(page, renewal_base_url, staff, day, root)
    race = _race_two_bookings(page, renewal_base_url, staff, day)
    _invalid_times(page, renewal_base_url, staff, day, root)
    replays = _stale_reschedule_and_replayed_cancel(
        page, renewal_base_url, staff, day, root
    )
    _replayed_booking_key(page, renewal_base_url, staff, day)
    physician_errors = _physician_view(page, renewal_base_url, staff, day, root)
    _cross_clinic(page, renewal_base_url, staff, day, root)
    # The only console entry is the refused clinic-C document itself.
    assert len(errors) == 1, errors
    assert re.search(r"\b404\b", errors[0])
    assert not physician_errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "agenda",
            "width": width,
            "assertion": "double booking refused with the fix named and inputs kept,"
            " then retried; two concurrent bookings of one window leave exactly one"
            " appointment; reversed, outside-window and cross-day times refused;"
            " stale reschedule refused after a cancel and the terminal state shown;"
            " cancel replay a no-op and a different reason refused; booking key"
            " replay creates no second row and other details conflict; physician"
            " reads only own rows with no control and POST refused; foreign clinic"
            " refused on agenda, transition and booking and never named; clinic B"
            " shows none of clinic A; no horizontal overflow in any of these states",
            "race": race,
            "cancel_replays": replays,
            "console_errors": errors,
        }
    )


# --------------------------------------------------------------------------
# Reflow and preferences
# --------------------------------------------------------------------------


def _seed_for_reflow(staff: dict[str, str]) -> None:
    _promise(staff, staff["physician_a_id"], (REFLOW_DAY, "08:00", "12:00"))
    _promise(staff, staff["physician_long_id"], (REFLOW_DAY, "13:00", "17:00"))
    _seed_rows(
        staff,
        [
            (
                "Helena Sintética Reflow",
                staff["physician_a_id"],
                REFLOW_DAY,
                "08:00",
                "08:30",
            ),
            (LONG_PATIENT, staff["physician_a_id"], REFLOW_DAY, "09:00", "09:45"),
            (
                UNBROKEN_PATIENT,
                staff["physician_long_id"],
                REFLOW_DAY,
                "13:00",
                "13:30",
            ),
        ],
    )


def _reflow(page: Page, root: Path) -> dict[str, object]:
    displays: dict[int, str] = {}
    heights: dict[int, float] = {}
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        displays[width] = str(
            page.evaluate(f"getComputedStyle(document.querySelector('{ROWS}')).display")
        )
        heights[width] = float(
            page.evaluate(
                f"document.querySelector('{ROWS} .agenda-actions a')"
                ".getBoundingClientRect().height"
            )
        )
        assert _no_overflow(page), width
        assert heights[width] >= MIN_TARGET_PX, (width, heights[width])
        assert len(_rows(page)) == 3
        _capture(page, root, f"list-{width}", full_page=False)
    assert displays[1280] == displays[768] == "table-row"
    assert displays[640] == displays[375] == displays[320] == "block"
    _capture(page, root, "long-320")
    report: dict[str, object] = {
        str(width): {"row_display": displays[width], "action_height": heights[width]}
        for width in WIDTHS
    }
    report["long_name_widths_without_overflow"] = list(WIDTHS)
    return report


def _forced_colors(page: Page, root: Path) -> dict[str, object]:
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    row_rule = page.evaluate(
        f"getComputedStyle(document.querySelector('{ROWS} th')).borderBottomStyle"
    )
    assert row_rule == "solid"
    current = page.evaluate(
        "getComputedStyle(document.querySelector('.agenda-views a[aria-current=page]'))"
        ".textDecorationLine"
    )
    assert current == "underline"
    page.locator(f"{ROWS} .agenda-actions a").first.focus()
    ring = _ring(page)
    assert ring["style"] == "solid"
    _capture(page, root, "list-forced-colors-1280", full_page=False)
    return {"row_rule": row_rule, "current_view": current, "ring": ring}


def _zoom_200(page: Page, root: Path) -> dict[str, object]:
    """A 1280px window at 200% browser zoom: 640 CSS px at device pixel ratio 2.

    Chromium implements page zoom as a device-scale change, so the context's
    ``device_scale_factor`` and halved viewport are the zoomed window itself;
    every capture here is 1280 device pixels wide with 200%-size text.
    """
    metrics = dict(
        page.evaluate(
            "({device_pixel_ratio: devicePixelRatio, css_viewport_width: innerWidth,"
            " root_font_size: getComputedStyle(document.documentElement).fontSize})"
        )
    )
    assert metrics["device_pixel_ratio"] == ZOOM_FACTOR, metrics
    assert metrics["css_viewport_width"] == ZOOM_WINDOW // ZOOM_FACTOR, metrics
    assert _no_overflow(page)
    row_display = str(
        page.evaluate(f"getComputedStyle(document.querySelector('{ROWS}')).display")
    )
    assert row_display == "block"
    height = float(
        page.evaluate(
            f"document.querySelector('{ROWS} .agenda-actions a')"
            ".getBoundingClientRect().height"
        )
    )
    assert height >= MIN_TARGET_PX, height
    captures = [_capture(page, root, "list-zoom-200")]
    with page.expect_navigation():
        page.locator(f"{ROWS} .agenda-actions a").first.click()
    expect(page.locator("h1")).to_have_text(gettext("Reschedule an appointment"))
    assert _no_overflow(page)
    captures.append(_capture(page, root, "reschedule-zoom-200"))
    return {
        **metrics,
        "window_width": ZOOM_WINDOW,
        "row_display": row_display,
        "action_height": height,
        "captures": captures,
    }


def test_reflow_forced_colors_reduced_motion_and_zoom_keep_the_agenda_usable(
    agenda_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
) -> None:
    root = renewal_artifact_root
    staff = agenda_staff
    agenda = _agenda_path(staff, "day", REFLOW_DAY)
    report: dict[str, object] = {}
    scenes: list[tuple[str, dict[str, Any]]] = [
        ("widths", {"viewport": {"width": 1280, "height": 900}}),
        (
            "forced_colors",
            {"viewport": {"width": 1280, "height": 900}, "forced_colors": "active"},
        ),
        (
            "reduced_motion",
            {"viewport": {"width": 375, "height": 900}, "reduced_motion": "reduce"},
        ),
        (
            "zoom_200",
            {
                "viewport": {
                    "width": ZOOM_WINDOW // ZOOM_FACTOR,
                    "height": 900 // ZOOM_FACTOR,
                },
                "device_scale_factor": ZOOM_FACTOR,
            },
        ),
    ]
    _seed_for_reflow(staff)
    for scene, options in scenes:
        context = agenda_browser.new_context(
            locale="pt-BR", timezone_id=BROWSER_ZONE, **options
        )
        page = context.new_page()
        page.set_default_timeout(20_000)
        try:
            _sign_in_receptionist(page, renewal_base_url, staff)
            page.goto(f"{renewal_base_url}{agenda}")
            if scene == "widths":
                report[scene] = _reflow(page, root)
            elif scene == "forced_colors":
                report[scene] = _forced_colors(page, root)
            elif scene == "zoom_200":
                report[scene] = _zoom_200(page, root)
            else:
                transition = page.evaluate(
                    "getComputedStyle(document.querySelector("
                    f"'{ROWS} .agenda-actions a'))"
                    ".transitionDuration"
                )
                assert transition == "0s"
                report[scene] = {"button_transition": transition}
        finally:
            context.close()
    report["captured_at"] = datetime.now(tz=UTC).isoformat()
    destination = root / "agenda" / "accessibility-report.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)
