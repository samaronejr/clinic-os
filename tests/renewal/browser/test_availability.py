"""Real-browser availability: create, read, retire, served as ``clinic_app``.

Owner access is confined to synthetic staff setup and to resetting the clinic
between journeys. Every wait subscribes to a navigation, a response, a held
request or a DOM state, never a timer. Captures contain synthetic names only.

Evidence grid: the two journey tests run once per matrix width (1280, 768 and
375) and name every capture ``<state>-<width>``, so each state (blank,
loading, success, grouped listing, long content, overlap, reversed, stale,
conflict, refused retirement, physician view, denied clinic) is rendered and
checked at all three widths and no capture overwrites another. The reflow
test adds 640 and 320 plus the preference scenes: forced colors, reduced
motion and a 1280px window at 200% browser zoom. Journey contexts run in the
Asia/Tokyo browser zone so every displayed hour is proven clinic-local.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.formats import date_format
from django.utils.translation import gettext, ngettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._page_wait import wait_for_js
from renewal.browser._protected import encrypt
from renewal.browser.engines import (
    assert_only_refused_document_logged,
    history_reload,
    new_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route

CLINIC_A = "Clínica Vila Mariana"
CLINIC_B = "Clínica Vila Olímpia"
CLINIC_ZONE = "America/Sao_Paulo"
BROWSER_ZONE = "Asia/Tokyo"
LONG_NAME = "dr-maximiliano-de-albuquerque-wanderley-sintetico-da-silva-neto-junior"
WIDTHS = (1280, 768, 640, 375, 320)
MATRIX_WIDTHS = (1280, 768, 375)
# Each matrix width works on its own civil days so a run never overlaps
# another run's periods and every journey starts from a cleared clinic.
DAYS = {
    1280: ("2031-03-04", "2031-03-05"),
    768: ("2031-03-11", "2031-03-12"),
    375: (
        "2031-03-18",
        "2031-03-19",
    ),
}
PAST_DAY = "2020-05-06"
ZOOM_WINDOW = 1280  # physical window width behind the 200% zoom scene
ZOOM_FACTOR = 2
OK = 200
NO_CONTENT = 204
SEE_OTHER = 303
NOT_FOUND = 404
MIN_TARGET_PX = 44
NAVY = "rgb(15, 45, 58)"
PRIMARY = "rgb(0, 122, 135)"
SUNKEN = "rgb(238, 235, 228)"  # button-disabled background
ERROR_TINT = "rgb(251, 237, 230)"
FORM = "#scheduling-panel"
SUBMIT = f"{FORM} button[type=submit]"
PROGRESS = "#scheduling-progress"
STATUS = "#availability-status"
ALERT = "#scheduling-errors"
RETIRE_ALERT = "#scheduling-retire-error"
ROWS = ".availability-window"
RETIRE_FORM = f"{ROWS} form"
RETIRE = f"{ROWS} button[type=submit]"
SETTLED_JS = (
    "!document.querySelector('.htmx-request, .htmx-settling, .htmx-added,"
    ' [aria-busy="true"]\')'
)
BUSY_JS = """
async ([form, button, progress]) => {
  const f = document.querySelector(form);
  const b = document.querySelector(button);
  const p = f.querySelector(progress) || document.querySelector(progress);
  // The disabled look transitions for --duration-1; record it settled.
  await Promise.all(b.getAnimations().map((animation) => animation.finished));
  const fs = getComputedStyle(f);
  const bs = getComputedStyle(b);
  const ps = getComputedStyle(p);
  return {
    aria_busy: f.getAttribute('aria-busy'),
    form_cursor: fs.cursor,
    form_border: fs.borderTopColor,
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
POST_SET = {OK, NO_CONTENT, 302, SEE_OTHER}


@pytest.fixture(scope="session")
def availability_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed a receptionist, three physicians (one TOTP-enrolled) and clinic B."""
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    suffix = uuid4().hex[:4]
    values = {
        "dsn": os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"],
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "clinic_b": str(uuid4()),
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "receptionist": f"recepcao-{suffix}",
        "receptionist_id": str(uuid4()),
        "physician_a": f"dra-ana-sintetica-{suffix}",
        "physician_a_id": str(uuid4()),
        "physician_b": f"dr-bruno-sintetico-{suffix}",
        "physician_b_id": str(uuid4()),
        "physician_long": LONG_NAME,
        "physician_long_id": str(uuid4()),
        "password": secrets.token_urlsafe(24),
        "totp_key": secrets.token_hex(20),
    }
    people = (
        (values["receptionist_id"], values["receptionist"]),
        (values["physician_a_id"], values["physician_a"]),
        (values["physician_b_id"], values["physician_b"]),
        (values["physician_long_id"], values["physician_long"]),
    )
    roles = (
        (values["receptionist_id"], values["clinic_a"], "receptionist"),
        (values["physician_a_id"], values["clinic_a"], "physician"),
        (values["physician_b_id"], values["clinic_a"], "physician"),
        (values["physician_long_id"], values["clinic_a"], "physician"),
        (values["physician_b_id"], values["clinic_b"], "physician"),
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
        connection.execute(
            "INSERT INTO clinic_app.identity_clinic "
            "(id, organization_id, name, crm_uf, timezone) "
            "VALUES (%s, %s, %s, 'SP', %s)",
            [values["clinic_b"], values["organization"], CLINIC_B, CLINIC_ZONE],
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
                    f"{username}@availability.invalid",
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
def availability_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


@pytest.fixture(params=MATRIX_WIDTHS, ids=[f"{width}px" for width in MATRIX_WIDTHS])
def journey(
    request: pytest.FixtureRequest, availability_browser: Browser
) -> Iterator[Page]:
    """One pt-BR context per matrix width, in a browser zone far from the clinic."""
    # Routed requests: WebKit's route() misses service-worker-controlled
    # pages (engines.py, Request interception).
    context = availability_browser.new_context(
        locale="pt-BR",
        timezone_id=BROWSER_ZONE,
        viewport={"width": int(request.param), "height": 900},
        service_workers="block",
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
    destination = root / "availability" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=full_page)
    destination.chmod(0o600)
    return destination.name


def _list_path(staff: dict[str, str], clinic: str = "clinic_a") -> str:
    return f"/scheduling/clinics/{staff[clinic]}/availability/"


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


def _clear_clinic(staff: dict[str, str]) -> None:
    """Reset fixtures via legal lifecycle transitions; never delete history."""
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "UPDATE clinic_app.scheduling_appointment "
            "SET status = 'cancelled', cancellation_reason = 'clinic_request', "
            "cancelled_at = now(), updated_at = now() "
            "WHERE clinic_id = %s AND status = 'scheduled'",
            [staff["clinic_a"]],
        )
        connection.execute(
            "UPDATE clinic_app.scheduling_availabilityblock SET retired_at = now() "
            "WHERE clinic_id = %s AND retired_at IS NULL",
            [staff["clinic_a"]],
        )


def _stored_bounds(staff: dict[str, str], day: str) -> list[tuple[datetime, datetime]]:
    """Return the UTC bounds stored for active periods that start on ``day``."""
    start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        rows = connection.execute(
            "SELECT start_at, end_at FROM clinic_app.scheduling_availabilityblock "
            "WHERE clinic_id = %s AND retired_at IS NULL "
            "AND start_at >= %s AND start_at < %s + interval '2 days' "
            "ORDER BY start_at",
            [staff["clinic_a"], start, start],
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _book_inside(staff: dict[str, str], practitioner_id: str, start_utc: str) -> None:
    """Insert one synthetic scheduled appointment (30 minutes) at ``start_utc``."""
    start = datetime.fromisoformat(start_utc)
    end = start.replace(minute=start.minute + 30)
    patient_id = str(uuid4())
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.intake_patient "
            "(id, organization_id, full_name, birth_date, created_at) "
            "VALUES (%s, %s, %s, %s, now())",
            [
                patient_id,
                staff["organization"],
                encrypt(
                    connection,
                    "intake.patient.full_name",
                    "Paciente Sintético Marcado".encode(),
                ),
                encrypt(connection, "intake.patient.birth_date", b"1990-01-01"),
            ],
        )
        connection.execute(
            "INSERT INTO clinic_app.intake_patientclinicenrollment "
            "(id, organization_id, clinic_id, patient_id, idempotency_key, "
            "create_fingerprint, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                patient_id,
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        connection.execute(
            "INSERT INTO clinic_app.scheduling_appointment "
            "(id, organization_id, clinic_id, patient_id, practitioner_id, start_at, "
            "end_at, idempotency_key, create_fingerprint, status, cancellation_reason, "
            "cancelled_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'scheduled', NULL, NULL, "
            "now(), now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                patient_id,
                practitioner_id,
                start,
                end,
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )


def _fill(page: Page, physician: str, day: str, start: str, end: str) -> None:
    page.locator("#id_practitioner").select_option(label=physician)
    page.locator("#id_local_date").fill(day)
    page.locator("#id_start_time").fill(start)
    page.locator("#id_end_time").fill(end)


def _submit_expecting_error(page: Page) -> None:
    """Submit over HTMX and wait for the swapped section to settle."""
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as received:
        page.locator(SUBMIT).click()
    assert received.value.status == OK, received.value.status
    wait_for_js(page, SETTLED_JS)


def _submit_expecting_success(page: Page, list_path: str) -> None:
    """Submit over HTMX; the 204 + HX-Redirect lands on the refreshed list."""
    with page.expect_navigation():
        page.locator(SUBMIT).click()
    page.wait_for_url(f"**{list_path}")


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


def _cookie_csrf(page: Page) -> str:
    return next(
        cookie.get("value", "")
        for cookie in page.context.cookies()
        if cookie.get("name") == "csrftoken"
    )


def _captions(page: Page) -> list[str]:
    """The physician captions in reading order, without their count badges."""
    captions: list[str] = page.locator(".availability-table caption").evaluate_all(
        "captions => captions.map(caption => caption.firstChild.textContent.trim())"
    )
    return captions


def _ledger(page: Page, index: int) -> Locator:
    """The ``index``-th (0-based) physician ledger in reading order."""
    return page.locator(".availability-practitioner").nth(index)


def _ledger_of(page: Page, physician: str) -> int:
    return _captions(page).index(physician)


def _day_headers(page: Page, index: int) -> list[str]:
    headers = _ledger(page, index).locator(".availability-day-row th")
    return [text.strip() for text in headers.all_inner_texts()]


def _ranges(page: Page, index: int) -> list[str]:
    rows = _ledger(page, index).locator(ROWS)
    return [
        f"{rows.nth(position).locator('th').inner_text().strip()}"
        f"-{rows.nth(position).locator('td').first.inner_text().strip()}"
        for position in range(rows.count())
    ]


def _row_button(page: Page, index: int, position: int) -> Locator:
    """The retire action of the ``position``-th row of the ``index``-th ledger."""
    return _ledger(page, index).locator(ROWS).nth(position).locator("button")


def _day_label(day: str) -> str:
    civil = date.fromisoformat(day)
    return f"{date_format(civil, 'l')}, {date_format(civil, 'SHORT_DATE_FORMAT')}"


def _status_text(total: int) -> str:
    return ngettext(
        "%(total)s active availability period.",
        "%(total)s active availability periods.",
        total,
    ) % {"total": total}


def _assert_autofocus_target(page: Page, element_id: str) -> None:
    """The one autofocus candidate is the named element, focusable by script."""
    candidates = page.locator("[autofocus]")
    expect(candidates).to_have_count(1)
    expect(candidates).to_have_id(element_id)
    expect(candidates).to_have_attribute("tabindex", "-1")


# --------------------------------------------------------------------------
# Happy journey steps
# --------------------------------------------------------------------------


def _open_blank(page: Page, base_url: str, list_path: str, root: Path) -> None:
    page.goto(f"{base_url}{list_path}")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    expect(page.locator("h1")).to_have_text(gettext("Physician availability"))
    expect(page.locator(".eyebrow")).to_have_count(0)
    expect(page.locator(".lede")).to_contain_text(CLINIC_ZONE)
    expect(page.locator(STATUS)).to_have_text(
        gettext("No active availability windows for this clinic.")
    )
    expect(page.locator(".availability-table")).to_have_count(0)
    expect(page.locator(".availability-empty")).to_be_visible()
    assert _no_overflow(page)
    _capture(page, root, f"blank-{_width(page)}")


def _create_first_period(
    page: Page, list_path: str, staff: dict[str, str], day: str, root: Path
) -> dict[str, object]:
    """Create one period with the POST held, land on the notice and the row."""
    width = _width(page)
    _fill(page, staff["physician_a"], day, "08:00", "09:00")
    with (
        _observed_in_flight(
            page, list_path, (FORM, SUBMIT, PROGRESS), f"loading-{width}", root
        ) as busy,
        page.expect_navigation(),
    ):
        page.locator(SUBMIT).click()
    page.wait_for_url(f"**{list_path}")
    _assert_busy(busy, gettext("Saving…"))
    assert busy["form_border"] == PRIMARY, busy  # .panel[aria-busy="true"]
    notice = page.locator("#availability-created")
    expect(notice).to_have_attribute("role", "status")
    expect(notice).to_contain_text(gettext("Period added"))
    expect(page.locator(STATUS)).to_have_text(_status_text(1))
    assert _captions(page) == [staff["physician_a"]]
    assert _day_headers(page, 0) == [_day_label(day)]
    assert _ranges(page, 0) == ["08:00-09:00"]
    assert "?" not in page.url
    assert day not in page.url
    assert _no_overflow(page)
    _capture(page, root, f"created-{width}")
    # Reload: the notice is consumed once and the row stays.
    page.reload()
    expect(page.locator("#availability-created")).to_have_count(0)
    assert _ranges(page, 0) == ["08:00-09:00"]
    return busy


def _create_grouped_periods(
    page: Page, list_path: str, staff: dict[str, str], days: tuple[str, str], root: Path
) -> None:
    """More periods: same physician later that day, next day, and another physician."""
    day, next_day = days
    for physician, civil, start, end in (
        (staff["physician_a"], day, "09:00", "12:00"),
        (staff["physician_a"], next_day, "14:00", "18:00"),
        (staff["physician_b"], day, "13:00", "17:00"),
    ):
        _fill(page, physician, civil, start, end)
        _submit_expecting_success(page, list_path)
    expect(page.locator(STATUS)).to_have_text(_status_text(4))
    # Physicians in name order, each with its own count; days in civil order;
    # rows in start order under their day.
    assert _captions(page) == sorted(
        [staff["physician_a"], staff["physician_b"]], key=str.casefold
    )
    ledger_a = _ledger_of(page, staff["physician_a"])
    ledger_b = _ledger_of(page, staff["physician_b"])
    badges = page.locator(".availability-table caption .badge").all_inner_texts()
    assert badges[ledger_a].strip() == ngettext(
        "%(total)s period", "%(total)s periods", 3
    ) % {"total": 3}
    assert _day_headers(page, ledger_a) == [_day_label(day), _day_label(next_day)]
    assert _ranges(page, ledger_a) == ["08:00-09:00", "09:00-12:00", "14:00-18:00"]
    assert _day_headers(page, ledger_b) == [_day_label(day)]
    assert _ranges(page, ledger_b) == ["13:00-17:00"]
    # Every displayed hour is clinic-local: the browser zone is Asia/Tokyo and
    # the stored instants are the São Paulo hours in UTC (-03:00 in March).
    stored = _stored_bounds(staff, day)
    assert stored == [
        (
            datetime.fromisoformat(f"{day}T11:00:00+00:00"),
            datetime.fromisoformat(f"{day}T12:00:00+00:00"),
        ),
        (
            datetime.fromisoformat(f"{day}T12:00:00+00:00"),
            datetime.fromisoformat(f"{day}T15:00:00+00:00"),
        ),
        (
            datetime.fromisoformat(f"{day}T16:00:00+00:00"),
            datetime.fromisoformat(f"{day}T20:00:00+00:00"),
        ),
        (
            datetime.fromisoformat(f"{next_day}T17:00:00+00:00"),
            datetime.fromisoformat(f"{next_day}T21:00:00+00:00"),
        ),
    ]
    assert all(bound.tzinfo is not None for pair in stored for bound in pair)
    assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == (
        BROWSER_ZONE
    )
    assert _no_overflow(page)
    _capture(page, root, f"grouped-{_width(page)}")


def _keyboard_reaches_the_row_action(
    page: Page, staff: dict[str, str], day: str, root: Path
) -> None:
    """Tab from the primary action into the first ledger and onto its first action."""
    page.locator(SUBMIT).focus()
    page.keyboard.press("Tab")
    assert _focused(page) == "table-scroll availability-practitioner"
    page.keyboard.press("Tab")
    label = str(page.evaluate("document.activeElement.textContent")).strip()
    assert label.startswith(gettext("Retire period"))
    first = _captions(page)[0]
    assert label.endswith(
        gettext("from %(start)s to %(end)s on %(day)s, %(practitioner)s")
        % {
            "start": "08:00" if first == staff["physician_a"] else "13:00",
            "end": "09:00" if first == staff["physician_a"] else "17:00",
            "day": date_format(date.fromisoformat(day), "SHORT_DATE_FORMAT"),
            "practitioner": first,
        }
    )
    ring = _ring(page)
    assert ring["style"] == "solid"
    assert ring["color"] == NAVY
    assert _no_overflow(page)
    _capture(page, root, f"focused-{_width(page)}")


def _retire_first_period(
    page: Page, list_path: str, staff: dict[str, str], root: Path
) -> dict[str, object]:
    """Retire the first row with the POST held; the list shrinks and says so."""
    width = _width(page)
    before = page.locator(ROWS).count()
    first_form = f".availability-practitioner {ROWS} form"
    first_button = f"{first_form} button[type=submit]"
    with (
        _observed_in_flight(
            page,
            "*/retire/",
            (first_form, first_button, ".htmx-indicator"),
            f"retiring-{width}",
            root,
        ) as busy,
        page.expect_navigation(),
    ):
        _row_button(page, 0, 0).click()
    page.wait_for_url(f"**{list_path}")
    _assert_busy(busy, gettext("Retiring…"))
    notice = page.locator("#availability-retired")
    expect(notice).to_have_attribute("role", "status")
    expect(notice).to_contain_text(gettext("Period retired"))
    expect(notice).to_contain_text(
        gettext(
            "No appointment was cancelled. To offer these hours again, "
            "add a new period."
        )
    )
    expect(page.locator(ROWS)).to_have_count(before - 1)
    expect(page.locator(STATUS)).to_have_text(_status_text(before - 1))
    assert "retire" not in page.url
    assert _no_overflow(page)
    _capture(page, root, f"retired-{width}")
    page.reload()
    expect(page.locator("#availability-retired")).to_have_count(0)
    del staff
    return busy


def _long_content(
    page: Page, list_path: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """A 70-character physician name and a full day of periods stay readable."""
    for start, end in (("07:00", "08:00"), ("08:00", "09:00"), ("09:00", "10:00")):
        _fill(page, staff["physician_long"], day, start, end)
        _submit_expecting_success(page, list_path)
    assert LONG_NAME in _captions(page)
    expect(
        page.locator(".availability-table caption").filter(has_text=LONG_NAME)
    ).to_have_count(1)
    assert _no_overflow(page)
    _capture(page, root, f"long-{_width(page)}")


def test_receptionist_creates_reads_and_retires_periods_by_physician_and_day(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    staff = availability_staff
    list_path = _list_path(staff)
    days = DAYS[width]
    _clear_clinic(staff)
    _sign_in_receptionist(page, renewal_base_url, staff)
    _open_blank(page, renewal_base_url, list_path, root)
    creating = _create_first_period(page, list_path, staff, days[0], root)
    _create_grouped_periods(page, list_path, staff, days, root)
    _keyboard_reaches_the_row_action(page, staff, days[0], root)
    retiring = _retire_first_period(page, list_path, staff, root)
    _long_content(page, list_path, staff, days[1], root)
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "availability",
            "width": width,
            "assertion": "blank clinic, create with the POST held (busy state),"
            " completion notice, grouping by physician then clinic-local day with"
            " counts and start order, clinic-local hours stored as UTC while the"
            " browser sits in Asia/Tokyo, keyboard focus order to the row action,"
            " retire with the POST held (busy state), retirement notice that names"
            " no cancelled appointment, long physician name",
            "browser_zone": BROWSER_ZONE,
            "clinic_zone": CLINIC_ZONE,
            "in_flight": {"create": creating, "retire": retiring},
            "console_errors": errors,
        }
    )


def test_native_post_creates_and_retires_without_javascript(
    availability_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    context: BrowserContext = availability_browser.new_context(
        locale="pt-BR",
        timezone_id=BROWSER_ZONE,
        viewport={"width": 768, "height": 1024},
        java_script_enabled=False,
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    staff = availability_staff
    list_path = _list_path(staff)
    root = renewal_artifact_root
    day = "2031-04-01"
    try:
        _clear_clinic(staff)
        _sign_in_receptionist(page, renewal_base_url, staff)
        page.goto(f"{renewal_base_url}{list_path}")
        _fill(page, staff["physician_a"], day, "08:00", "09:00")
        with page.expect_navigation():
            page.locator(SUBMIT).click()
        assert page.url == f"{renewal_base_url}{list_path}"
        expect(page.locator("#availability-created")).to_be_visible()
        assert _ranges(page, 0) == ["08:00-09:00"]
        assert _day_headers(page, 0) == [_day_label(day)]
        assert _no_overflow(page)
        _capture(page, root, "native-created-768", full_page=False)

        # Native validation error: the summary is the focus target, inputs kept.
        _fill(page, staff["physician_a"], day, "10:00", "09:30")
        with page.expect_navigation():
            page.locator(SUBMIT).click()
        _assert_autofocus_target(page, "scheduling-errors")
        expect(page.locator(ALERT)).to_contain_text(
            gettext("Enter a future clinic-local window that ends after it starts.")
        )
        expect(page.locator("#id_start_time")).to_have_value("10:00")
        expect(page.locator("#id_end_time")).to_have_value("09:30")
        expect(page.locator("#id_local_date")).to_have_value(day)
        expect(page.locator("#availability-created")).to_have_count(0)
        assert _ranges(page, 0) == ["08:00-09:00"]
        _capture(page, root, "native-invalid-768", full_page=False)

        # Native retirement: one POST, one redirect, the row is gone.
        with page.expect_navigation():
            page.locator(RETIRE).first.click()
        assert page.url == f"{renewal_base_url}{list_path}"
        expect(page.locator("#availability-retired")).to_be_visible()
        expect(page.locator(ROWS)).to_have_count(0)
        expect(page.locator(STATUS)).to_have_text(
            gettext("No active availability windows for this clinic.")
        )
        _capture(page, root, "native-retired-768", full_page=False)
    finally:
        context.close()


# --------------------------------------------------------------------------
# Failure journey steps
# --------------------------------------------------------------------------


def _overlapping(
    page: Page, list_path: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """A range inside a promised one is refused; the retry with a free range lands."""
    width = _width(page)
    _fill(page, staff["physician_a"], day, "08:30", "09:30")
    _submit_expecting_error(page)
    alert = page.locator(ALERT)
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text(gettext("We could not add that availability"))
    expect(alert).to_contain_text(
        gettext("That physician already promises part of this window.")
    )
    assert _focused(page) == "scheduling-errors"
    expect(page.locator("#id_local_date")).to_have_value(day)
    expect(page.locator("#id_start_time")).to_have_value("08:30")
    expect(page.locator("#id_end_time")).to_have_value("09:30")
    expect(page.locator("#id_practitioner")).to_have_value(staff["physician_a_id"])
    expect(page.locator(ROWS)).to_have_count(1)
    assert _no_overflow(page)
    _capture(page, root, f"overlap-{width}")
    page.locator("#id_start_time").fill("09:00")
    page.locator("#id_end_time").fill("10:00")
    _submit_expecting_success(page, list_path)
    expect(page.locator("#availability-created")).to_be_visible()
    assert _ranges(page, 0) == ["08:00-09:00", "09:00-10:00"]


def _reversed(
    page: Page, list_path: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """An end before its start is refused with the fix named; the retry lands."""
    width = _width(page)
    _fill(page, staff["physician_a"], day, "16:00", "15:00")
    _submit_expecting_error(page)
    expect(page.locator(ALERT)).to_contain_text(
        gettext("Enter a future clinic-local window that ends after it starts.")
    )
    assert _focused(page) == "scheduling-errors"
    expect(page.locator("#availability-created")).to_have_count(0)
    expect(page.locator("#id_start_time")).to_have_value("16:00")
    expect(page.locator("#id_end_time")).to_have_value("15:00")
    assert _no_overflow(page)
    _capture(page, root, f"reversed-{width}")
    page.locator("#id_end_time").fill("17:00")
    _submit_expecting_success(page, list_path)
    assert _ranges(page, 0) == ["08:00-09:00", "09:00-10:00", "16:00-17:00"]


def _stale(
    page: Page, list_path: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """A past date and a reused key with other data are refused; the retry lands."""
    width = _width(page)
    _fill(page, staff["physician_b"], PAST_DAY, "08:00", "09:00")
    _submit_expecting_error(page)
    expect(page.locator(ALERT)).to_contain_text(
        gettext("Enter a future clinic-local window that ends after it starts.")
    )
    expect(page.locator("#id_local_date")).to_have_value(PAST_DAY)
    assert _no_overflow(page)
    _capture(page, root, f"stale-{width}")

    # The same key already used for another period: refused, nothing changes.
    key = page.locator("input[name=idempotency_key]").get_attribute("value")
    assert key
    replay = page.request.post(
        f"{page.url.split('/scheduling/')[0]}{list_path}",
        form={
            "csrfmiddlewaretoken": _csrf(page),
            "practitioner": staff["physician_b_id"],
            "local_date": day,
            "start_time": "13:00",
            "end_time": "14:00",
            "idempotency_key": key,
        },
        headers={"Referer": page.url},
        max_redirects=0,
    )
    assert replay.status == SEE_OTHER
    page.locator("#id_local_date").fill(day)
    page.locator("#id_start_time").fill("13:00")
    page.locator("#id_end_time").fill("15:00")
    _submit_expecting_error(page)
    expect(page.locator(ALERT)).to_contain_text(
        gettext("This availability was already submitted differently.")
    )
    assert _no_overflow(page)
    _capture(page, root, f"conflict-{width}")
    page.reload()
    _fill(page, staff["physician_b"], day, "14:00", "15:00")
    _submit_expecting_success(page, list_path)
    ledger_b = _ledger_of(page, staff["physician_b"])
    assert _ranges(page, ledger_b) == ["13:00-14:00", "14:00-15:00"]


def _refused_retirement(
    page: Page, list_path: str, staff: dict[str, str], day: str, root: Path
) -> None:
    """A period holding a future appointment stays; the refused row is marked."""
    width = _width(page)
    _book_inside(staff, staff["physician_a_id"], f"{day}T11:00:00+00:00")
    ledger_a = _ledger_of(page, staff["physician_a"])
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and "/retire/" in response.url
        )
    ) as refused:
        _row_button(page, ledger_a, 0).click()
    assert refused.value.status == OK
    wait_for_js(page, SETTLED_JS)
    alert = page.locator(RETIRE_ALERT)
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text(
        gettext("This block still has future appointments and cannot be retired.")
    )
    assert _focused(page) == "scheduling-retire-error"
    marked = page.locator(f"{ROWS}.table-row--error")
    expect(marked).to_have_count(1)
    expect(marked.locator("th")).to_have_text("08:00")
    expect(marked.locator(".badge--error")).to_have_text(gettext("Has appointments"))
    background = marked.evaluate("el => getComputedStyle(el).backgroundColor")
    assert background == ERROR_TINT
    assert _ranges(page, ledger_a)[0] == "08:00-09:00"
    assert "?" not in page.url
    assert _no_overflow(page)
    _capture(page, root, f"retire-refused-{width}")
    # A repeated retirement of an already retired period is a quiet no-op.
    second_button = _row_button(page, ledger_a, 1)
    action = second_button.locator("xpath=ancestor::form").get_attribute("action")
    assert action
    with page.expect_navigation():
        second_button.click()
    expect(page.locator("#availability-retired")).to_be_visible()
    replay = page.request.post(
        f"{page.url.split('/scheduling/')[0]}{action}",
        form={"csrfmiddlewaretoken": _csrf(page)},
        headers={"Referer": page.url},
        max_redirects=0,
    )
    assert replay.status == SEE_OTHER
    assert replay.headers["location"] == list_path


def _physician_view(
    source: Page,
    base_url: str,
    staff: dict[str, str],
    day: str,
    root: Path,
) -> list[str]:
    """The physician reads only their own periods and holds no write control."""
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
    list_path = _list_path(staff)
    try:
        _sign_in_physician(page, base_url, staff)
        page.goto(f"{base_url}{list_path}")
        expect(page.locator("h1")).to_have_text(gettext("Physician availability"))
        expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
        expect(page.locator("form")).to_have_count(0)
        expect(page.locator("button")).to_have_count(0)
        expect(page.locator(FORM)).to_have_count(0)
        assert _captions(page) == [staff["physician_a"]]
        assert staff["physician_b"] not in page.content()
        assert _day_headers(page, 0) == [_day_label(day)]
        assert _ranges(page, 0) == ["08:00-09:00", "16:00-17:00"]
        expect(page.locator(STATUS)).to_have_text(_status_text(2))
        assert _no_overflow(page)
        _capture(page, root, f"physician-{width}")
        # Write attempts by the physician are refused, never offered.
        csrf = _cookie_csrf(page)
        created = page.request.post(
            f"{base_url}{list_path}",
            form={
                "csrfmiddlewaretoken": csrf,
                "practitioner": staff["physician_a_id"],
                "local_date": day,
                "start_time": "18:00",
                "end_time": "19:00",
                "idempotency_key": str(uuid4()),
            },
            headers={"Referer": f"{base_url}{list_path}"},
        )
        assert created.status == NOT_FOUND
        # This context keeps its service worker; reload the way the document
        # does (engines.history_reload: Firefox's page.reload() never reports
        # load once a worker is registered).
        history_reload(page)
        assert _ranges(page, 0) == ["08:00-09:00", "16:00-17:00"]
    finally:
        context.close()
    return errors


def _foreign_clinic(
    page: Page, base_url: str, staff: dict[str, str], root: Path
) -> None:
    """No role in clinic B: its availability is refused on GET and POST, never named."""
    list_b = _list_path(staff, "clinic_b")
    response = page.goto(f"{base_url}{list_b}")
    assert response is not None
    assert response.status == NOT_FOUND
    expect(page.locator("h1")).to_have_text(gettext("Page unavailable"))
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    assert CLINIC_B not in page.content()
    assert _no_overflow(page)
    _capture(page, root, f"foreign-clinic-denied-{_width(page)}")
    page.goto(f"{base_url}{_list_path(staff)}")
    refused = page.request.post(
        f"{base_url}{list_b}",
        form={
            "csrfmiddlewaretoken": _csrf(page),
            "practitioner": staff["physician_b_id"],
            "local_date": "2031-05-05",
            "start_time": "08:00",
            "end_time": "09:00",
            "idempotency_key": str(uuid4()),
        },
        headers={"Referer": f"{base_url}{_list_path(staff)}"},
    )
    assert refused.status == NOT_FOUND
    assert CLINIC_B not in refused.text()


def test_failures_show_actionable_errors_and_keep_permission_boundaries(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    staff = availability_staff
    list_path = _list_path(staff)
    day = DAYS[width][0].replace("-03-", "-04-")  # April days keep runs apart
    _clear_clinic(staff)
    _sign_in_receptionist(page, renewal_base_url, staff)
    page.goto(f"{renewal_base_url}{list_path}")
    _fill(page, staff["physician_a"], day, "08:00", "09:00")
    _submit_expecting_success(page, list_path)
    _overlapping(page, list_path, staff, day, root)
    _reversed(page, list_path, staff, day, root)
    _stale(page, list_path, staff, day, root)
    _refused_retirement(page, list_path, staff, day, root)
    physician_errors = _physician_view(page, renewal_base_url, staff, day, root)
    _foreign_clinic(page, renewal_base_url, staff, root)
    # The only console entry is the refused clinic-B document itself (where
    # the engine logs failed responses at all).
    assert_only_refused_document_logged(page, errors, "404")
    assert not physician_errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "availability",
            "width": width,
            "assertion": "overlapping, reversed, past and conflicting-key submits"
            " refused with the fix named and inputs kept, each retried to success;"
            " retirement refused for a period with a future appointment and the"
            " row marked; repeated retirement a no-op; physician sees only own"
            " periods and no control, POST refused; foreign clinic refused and"
            " never named; no horizontal overflow in any of these states",
            "console_errors": errors,
        }
    )


# --------------------------------------------------------------------------
# Reflow and preferences
# --------------------------------------------------------------------------


def _seed_for_reflow(page: Page, list_path: str, staff: dict[str, str]) -> None:
    for physician, day, start, end in (
        (staff["physician_a"], "2031-05-06", "08:00", "09:00"),
        (staff["physician_a"], "2031-05-07", "09:00", "12:00"),
        (staff["physician_long"], "2031-05-06", "13:00", "17:00"),
    ):
        _fill(page, physician, day, start, end)
        _submit_expecting_success(page, list_path)


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
                f"document.querySelector('{RETIRE}').getBoundingClientRect().height"
            )
        )
        assert _no_overflow(page), width
        assert heights[width] >= MIN_TARGET_PX, (width, heights[width])
        expect(
            page.locator(".availability-table caption").filter(has_text=LONG_NAME)
        ).to_have_count(1)
        _capture(page, root, f"list-{width}", full_page=False)
    assert displays[1280] == displays[768] == "table-row"
    assert displays[640] == displays[375] == displays[320] == "flex"
    _capture(page, root, "long-320")
    report: dict[str, object] = {
        str(width): {"row_display": displays[width], "button_height": heights[width]}
        for width in WIDTHS
    }
    report["long_name_widths_without_overflow"] = list(WIDTHS)
    return report


def _forced_colors(page: Page, root: Path) -> dict[str, object]:
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    day_rule = page.evaluate(
        "getComputedStyle(document.querySelector("
        "'.availability-table tbody:nth-of-type(2) .availability-day-row th'))"
        ".borderTopStyle"
    )
    assert day_rule == "solid"
    row_rule = page.evaluate(
        f"getComputedStyle(document.querySelector('{ROWS} th')).borderBottomStyle"
    )
    assert row_rule == "solid"
    page.locator(RETIRE).first.focus()
    ring = _ring(page)
    assert ring["style"] == "solid"
    _capture(page, root, "list-forced-colors-1280", full_page=False)
    return {"day_rule": day_rule, "row_rule": row_rule, "ring": ring}


def _zoom_200(
    page: Page, list_path: str, staff: dict[str, str], root: Path
) -> dict[str, object]:
    """A 1280px window at 200% browser zoom: 640 CSS px at device pixel ratio 2.

    Chromium implements page zoom as a device-scale change, so the context's
    ``device_scale_factor`` and halved viewport are the zoomed window itself;
    every capture here is 1280 device pixels wide with 200%-size text.
    """
    del list_path
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
    assert row_display == "flex"
    height = float(
        page.evaluate(
            f"document.querySelector('{RETIRE}').getBoundingClientRect().height"
        )
    )
    assert height >= MIN_TARGET_PX, height
    captures = [_capture(page, root, "list-zoom-200")]
    _fill(page, staff["physician_a"], "2031-05-06", "08:30", "09:30")
    _submit_expecting_error(page)
    expect(page.locator(ALERT)).to_be_visible()
    assert _focused(page) == "scheduling-errors"
    assert _no_overflow(page)
    captures.append(_capture(page, root, "overlap-zoom-200"))
    return {
        **metrics,
        "window_width": ZOOM_WINDOW,
        "row_display": row_display,
        "button_height": height,
        "captures": captures,
    }


def test_reflow_forced_colors_reduced_motion_and_zoom_keep_availability_usable(
    availability_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    root = renewal_artifact_root
    staff = availability_staff
    list_path = _list_path(staff)
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
    _clear_clinic(staff)
    for scene, options in scenes:
        context = new_context(
            availability_browser, locale="pt-BR", timezone_id=BROWSER_ZONE, **options
        )
        page = context.new_page()
        page.set_default_timeout(20_000)
        try:
            _sign_in_receptionist(page, renewal_base_url, staff)
            page.goto(f"{renewal_base_url}{list_path}")
            if scene == "widths":
                _seed_for_reflow(page, list_path, staff)
                report[scene] = _reflow(page, root)
            elif scene == "forced_colors":
                report[scene] = _forced_colors(page, root)
            elif scene == "zoom_200":
                report[scene] = _zoom_200(page, list_path, staff, root)
            else:
                transition = page.evaluate(
                    f"getComputedStyle(document.querySelector('{SUBMIT}'))"
                    ".transitionDuration"
                )
                assert transition == "0s"
                report[scene] = {"button_transition": transition}
        finally:
            context.close()
    report["captured_at"] = datetime.now(tz=UTC).isoformat()
    destination = root / "availability" / "accessibility-report.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)
