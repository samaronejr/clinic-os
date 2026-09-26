"""Real-browser staff intake: find, register and retry, served as ``clinic_app``.

Owner access is confined to synthetic staff and registry setup. Every wait
subscribes to a navigation, a response, a held request or a DOM state, never
a timer. Captures contain synthetic names only.

Evidence grid: the two journey tests run once per matrix width (1280, 768 and
375) and name every capture ``<surface>-<state>-<width>``, so each state
(default, loading, success, long content, empty, error, conflict, denied) is
rendered and checked at all three widths and no capture overwrites another.
The reflow test adds 640 and 320 plus the preference scenes: forced colors,
reduced motion and a 1280px window at 200% browser zoom.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext, ngettext
from playwright.sync_api import expect

from renewal.browser._page_wait import wait_for_js
from renewal.browser._protected import encrypt

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Browser, BrowserContext, Page, Route

CLINIC_A = "Clínica Vila Mariana"
CLINIC_B = "Clínica Vila Olímpia"
SEEDED = 30  # "Marina Sintética Pnnn" enrollments in clinic A
PAGE_SIZE = 25
LONG_NAME = (
    "Maria Aparecida Conceição dos Santos Oliveira Ferreira de Albuquerque Sintética"
)
UNBROKEN_NAME = "Wolfeschlegelsteinhausenbergerdorff Sintética"
FOREIGN_NAME = "Marina Sintética Estrangeira"
WIDTHS = (1280, 768, 640, 375, 320)
MATRIX_WIDTHS = (1280, 768, 375)
ZOOM_WINDOW = 1280  # physical window width behind the 200% zoom scene
ZOOM_FACTOR = 2
OK = 200
SEE_OTHER = 303
NOT_FOUND = 404
MIN_TARGET_PX = 44
NAVY = "rgb(15, 45, 58)"
PRIMARY = "rgb(0, 122, 135)"
SUNKEN = "rgb(238, 235, 228)"  # button-disabled background
SEARCH_FORM = "#patient-search-form"
SEARCH = f"{SEARCH_FORM} button[type=submit]"
REGISTER_FORM = "#patient-create-panel form"
REGISTER = "#patient-create-panel button[type=submit]"
FORMS = {"search": (SEARCH_FORM, SEARCH), "register": (REGISTER_FORM, REGISTER)}
PROGRESS = "#intake-progress"
STATUS = "#patient-results-status"
ALERT = "#intake-errors"
SETTLED_JS = (
    "!document.querySelector('.htmx-request, .htmx-settling, .htmx-added,"
    ' [aria-busy="true"]\')'
)
BUSY_JS = """
async ([form, button]) => {
  const f = document.querySelector(form);
  const b = document.querySelector(button);
  const p = document.querySelector('#intake-progress');
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
    progress_classes: p.className,
    progress_display: ps.display,
    progress_visibility: ps.visibility,
    progress_opacity: ps.opacity,
  };
}
"""
POST_SET = {OK, 204, 302, SEE_OTHER}
BLANK = "Submit a search to list patients."


@pytest.fixture(scope="session")
def intake_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed a clinic-A receptionist, a registry in A and one patient in clinic B.

    Session scope: every test counts rows of this one registry.
    """
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    values = {
        "dsn": os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"],
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "clinic_b": str(uuid4()),
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "receptionist": f"recepcao-{uuid4().hex[:8]}",
        "receptionist_id": str(uuid4()),
        "password": secrets.token_urlsafe(24),
    }
    registry = [
        (
            values["clinic_a"],
            f"Marina Sintética P{index:03d}",
            f"1990-01-{1 + index % 27:02d}",
        )
        for index in range(SEEDED)
    ]
    registry += [
        (values["clinic_a"], "Marina Sintética P000", "1975-06-15"),
        (values["clinic_a"], LONG_NAME, "1958-11-30"),
        (values["clinic_a"], UNBROKEN_NAME, "1970-02-01"),
        (values["clinic_b"], FOREIGN_NAME, "1988-03-12"),
    ]
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
            "VALUES (%s, %s, %s, 'SP', 'America/Sao_Paulo')",
            [values["clinic_b"], values["organization"], CLINIC_B],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id, username, password, email, first_name, last_name, is_active, "
            "is_staff, is_superuser, date_joined) "
            "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
            [
                values["receptionist_id"],
                values["receptionist"],
                make_password(values["password"]),
                f"{values['receptionist']}@intake.invalid",
            ],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id, user_id, organization_id, clinic_id, role) "
            "VALUES (%s, %s, %s, %s, 'receptionist')",
            [
                str(uuid4()),
                values["receptionist_id"],
                values["organization"],
                values["clinic_a"],
            ],
        )
        for clinic_id, name, birth_date in registry:
            patient_id = str(uuid4())
            connection.execute(
                "INSERT INTO clinic_app.intake_patient "
                "(id, organization_id, full_name, birth_date, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                [
                    patient_id,
                    values["organization"],
                    encrypt(connection, "intake.patient.full_name", name.encode()),
                    encrypt(
                        connection,
                        "intake.patient.birth_date",
                        birth_date.encode("ascii"),
                    ),
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.intake_patientclinicenrollment "
                "(id, organization_id, clinic_id, patient_id, idempotency_key, "
                "create_fingerprint, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now())",
                [
                    str(uuid4()),
                    values["organization"],
                    clinic_id,
                    patient_id,
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
    return values


@pytest.fixture
def intake_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


@pytest.fixture(params=MATRIX_WIDTHS, ids=[f"{width}px" for width in MATRIX_WIDTHS])
def journey(request: pytest.FixtureRequest, intake_browser: Browser) -> Iterator[Page]:
    """One pt-BR context per matrix width; tests read the width back from the page."""
    context = intake_browser.new_context(
        locale="pt-BR", viewport={"width": int(request.param), "height": 900}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    yield page
    context.close()


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
    destination = root / "staff-intake" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=full_page)
    destination.chmod(0o600)
    return destination.name


def _submit(page: Page, selector: str) -> int:
    """Click one submit control and wait for its POST plus the HTMX settle."""
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as received:
        page.locator(selector).click()
    assert received.value.status in POST_SET, received.value.status
    wait_for_js(page, SETTLED_JS)
    return received.value.status


def _search(page: Page, query: str, *, birth_date: str = "") -> None:
    page.locator("#id_q").fill(query)
    page.locator("#id_birth_date").fill(birth_date)
    _submit(page, SEARCH)


@contextlib.contextmanager
def _observed_in_flight(
    page: Page, path: str, surface: str, root: Path
) -> Iterator[dict[str, object]]:
    """Hold the next POST to ``path`` while its busy state is recorded and captured.

    The route handler runs while the request is paused, so what it records is
    the in-flight state by construction; the handler releases the request in
    its ``finally`` and the caller asserts on the record once the response or
    navigation it subscribed to has arrived. The capture is named
    ``<surface>-loading-<width>``.
    """
    observed: dict[str, object] = {}
    form, button = FORMS[surface]

    def hold(route: Route) -> None:
        if route.request.method != "POST" or observed:
            route.continue_()
            return
        try:
            observed.update(page.evaluate(BUSY_JS, [form, button]))
            observed["capture"] = _capture(
                page, root, f"{surface}-loading-{_width(page)}"
            )
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
    assert "htmx-request" in str(busy["progress_classes"]).split(), busy
    # The indicator is a flex item, so its `inline-flex` computes to `flex`.
    assert busy["progress_display"] != "none", busy
    assert busy["progress_visibility"] == "visible", busy
    assert busy["progress_opacity"] == "1", busy


def _search_in_flight(
    page: Page, patients: str, query: str, root: Path
) -> dict[str, object]:
    """Search once with the POST held: the panel is busy and says so, then settles."""
    page.locator("#id_q").fill(query)
    page.locator("#id_birth_date").fill("")
    with (
        _observed_in_flight(page, patients, "search", root) as busy,
        page.expect_response(
            lambda response: response.request.method == "POST"
        ) as received,
    ):
        page.locator(SEARCH).click()
    assert received.value.status == OK
    wait_for_js(page, SETTLED_JS)
    _assert_busy(busy, gettext("Searching…"))
    assert busy["form_border"] == PRIMARY, busy  # .panel[aria-busy="true"]
    expect(page.locator(SEARCH)).to_be_enabled()
    expect(page.locator(PROGRESS)).to_be_hidden()
    return busy


def _sign_in(page: Page, base_url: str, staff: dict[str, str]) -> None:
    page.goto(f"{base_url}/auth/login/")
    page.locator("#id_username").fill(staff["receptionist"])
    page.locator("#id_password").fill(staff["password"])
    with page.expect_navigation():
        page.locator("button[type=submit]").click()
    page.wait_for_url("**/auth/protected/")


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


def _overflow_offenders(page: Page) -> list[str]:
    """List elements wider than the viewport, for a readable failure."""
    offenders = page.evaluate(
        "(() => {"
        "const vw = document.documentElement.clientWidth;"
        "const clipped = (el) => {"
        "for (let p = el; p; p = p.parentElement) {"
        "const cs = getComputedStyle(p);"
        "if (cs.overflowX !== 'visible') return true;"
        "if (cs.clipPath && cs.clipPath !== 'none') return true;"
        "}"
        "return false;"
        "};"
        "const rows = [...document.querySelectorAll('body *')]"
        ".filter((el) => !clipped(el)"
        "  && (el.getBoundingClientRect().right > vw + 1"
        "    || el.getBoundingClientRect().left < -1))"
        ".slice(0, 8)"
        ".map((el) => el.tagName + '.' + String(el.className)"
        "+ ' right=' + el.getBoundingClientRect().right.toFixed(0));"
        "rows.push('doc scrollWidth='"
        "+ document.documentElement.scrollWidth + ' vw=' + vw);"
        "return rows;"
        "})()"
    )
    assert isinstance(offenders, list)
    result = [str(item) for item in offenders]
    if not _no_overflow(page):  # pragma: no cover - failure diagnostics only
        Path("/tmp/t17-overflow-debug.html").write_text(  # noqa: S108
            page.content(), encoding="utf-8"
        )
    return result


def _row_names(page: Page) -> list[str]:
    names = page.locator(".intake-table tbody th").all_inner_texts()
    return [name.strip() for name in names]


def _csrf(page: Page) -> str:
    value = page.locator("input[name=csrfmiddlewaretoken]").first.get_attribute("value")
    assert value
    return value


def _status_text(total: int, page_number: int, pages: int) -> str:
    count = ngettext(
        "%(total)s matching patient.", "%(total)s matching patients.", total
    ) % {"total": total}
    where = gettext("Page %(page)s of %(pages)s.") % {
        "page": page_number,
        "pages": pages,
    }
    return f"{count} {where}"


def _assert_autofocus_target(page: Page, element_id: str) -> None:
    """The one autofocus candidate is the named element, focusable by script."""
    candidates = page.locator("[autofocus]")
    expect(candidates).to_have_count(1)
    expect(candidates).to_have_id(element_id)
    expect(candidates).to_have_attribute("tabindex", "-1")


def _remove_attribute(page: Page, selector: str, attribute: str) -> None:
    """Lift one native constraint so the server-side validation is exercised."""
    page.locator(selector).evaluate(
        "(element, name) => element.removeAttribute(name)", attribute
    )


# --------------------------------------------------------------------------
# Happy journey steps
# --------------------------------------------------------------------------


def _open_blank_search(page: Page, base_url: str, patients: str, root: Path) -> None:
    page.goto(f"{base_url}{patients}")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    expect(page.locator("h1")).to_have_text(gettext("Patient search"))
    expect(page.locator(".eyebrow")).to_have_count(0)
    expect(page.locator(STATUS)).to_have_text(gettext(BLANK))
    expect(page.locator(".intake-table")).to_have_count(0)
    assert _no_overflow(page)
    _capture(page, root, f"search-blank-{_width(page)}")


def _register_and_land(
    page: Page, patients: str, name: str, root: Path
) -> dict[str, object]:
    """Register through the secondary action, hold the POST, land on the notice."""
    width = _width(page)
    with page.expect_navigation():
        page.locator(f"{SEARCH_FORM} a.button--secondary").click()
    expect(page.locator("h1")).to_have_text(gettext("Register a patient"))
    assert _no_overflow(page)
    _capture(page, root, f"register-blank-{width}")
    page.locator("#id_full_name").fill(name)
    page.locator("#id_birth_date").fill("1990-05-17")
    with (
        _observed_in_flight(page, f"{patients}new/", "register", root) as busy,
        page.expect_navigation(),
    ):
        page.locator(REGISTER).click()
    page.wait_for_url(f"**{patients}")
    _assert_busy(busy, gettext("Registering…"))
    notice = page.locator("#intake-registered")
    expect(notice).to_have_attribute("role", "status")
    expect(notice).to_contain_text(gettext("Patient registered"))
    body = page.content()
    assert name not in body
    assert "1990" not in body
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    assert _no_overflow(page)
    _capture(page, root, f"search-registered-{width}")
    # Reload: the notice is consumed once and the search stays blank.
    page.reload()
    expect(page.locator("#intake-registered")).to_have_count(0)
    expect(page.locator(STATUS)).to_have_text(gettext(BLANK))
    return busy


def _find_across_pages(
    page: Page, base_url: str, patients: str, name: str, root: Path
) -> dict[str, object]:
    """Find the new patient, then walk a two-page registry by keyboard."""
    width = _width(page)
    status = page.locator(STATUS)
    busy = _search_in_flight(page, patients, name, root)
    expect(status).to_have_text(_status_text(1, 1, 1))
    assert _row_names(page) == [name]
    total = SEEDED + 1  # the seeded registry plus the same-name duplicate
    _search(page, "Marina Sintética")
    expect(status).to_have_text(_status_text(total, 1, 2))
    assert _focused(page) == "patient-results-status"
    names = _row_names(page)
    assert len(names) == PAGE_SIZE
    assert names == sorted(names, key=str.casefold)
    expect(page.locator(".table-scroll")).to_have_attribute(
        "aria-labelledby", "patient-results-caption"
    )
    page.keyboard.press("Tab")
    assert _focused(page) == "table-scroll"
    page.keyboard.press("Tab")
    first_action = str(page.evaluate("document.activeElement.textContent"))
    assert first_action.startswith(gettext("Book appointment"))
    assert first_action.endswith(gettext("for %(name)s") % {"name": names[0]})
    ring = _ring(page)
    assert ring["style"] == "solid"
    assert ring["color"] == NAVY
    assert _no_overflow(page)
    _capture(page, root, f"search-results-focused-{width}")

    # Second page by keyboard: query and page travel in the body, never the URL.
    page.locator(".intake-pagination button").last.focus()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.request.post_data is not None
            and "page=2" in response.request.post_data
        )
    ) as second:
        page.keyboard.press("Enter")
    assert second.value.status == OK
    wait_for_js(page, SETTLED_JS)
    expect(status).to_have_text(_status_text(total, 2, 2))
    assert _focused(page) == "patient-results-status"
    assert len(_row_names(page)) == total - PAGE_SIZE
    assert page.url == f"{base_url}{patients}"
    assert _no_overflow(page)
    _capture(page, root, f"search-page-2-{width}")

    # Reload after a body-only search: no stale result and no query in the URL.
    page.reload()
    assert page.url == f"{base_url}{patients}"
    expect(page.locator(".intake-table")).to_have_count(0)
    expect(status).to_have_text(gettext(BLANK))
    return {"total": total, "search_in_flight": busy}


def _disambiguate_same_name(page: Page) -> None:
    """Same-name patients are told apart by the birth date cell only."""
    status = page.locator(STATUS)
    _search(page, "Marina Sintética P000")
    assert _row_names(page) == ["Marina Sintética P000", "Marina Sintética P000"]
    dates = page.locator(".intake-table tbody td[data-label]").all_inner_texts()
    assert sorted(date.split(": ")[-1] for date in dates) == [
        "01/01/1990",
        "15/06/1975",
    ]
    _search(page, "Marina Sintética P000", birth_date="1975-06-15")
    assert _row_names(page) == ["Marina Sintética P000"]
    expect(status).to_have_text(_status_text(1, 1, 1))
    assert "1975" not in status.inner_text()
    assert "1975" not in page.locator("caption").inner_text()


def _book_handoff(page: Page, name: str, root: Path) -> None:
    """The row action posts the enrollment and the booking names the patient."""
    _search(page, name)
    with page.expect_navigation():
        page.locator(".intake-table tbody button").first.click()
    assert page.url.endswith("/appointments/new/")
    assert "?" not in page.url
    expect(page.locator("#booking-patient")).to_have_text(name)
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    _capture(page, root, f"booking-handoff-{_width(page)}", full_page=False)


def test_receptionist_registers_and_finds_a_patient_across_pagination_and_reload(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    intake_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    patients = f"/intake/clinics/{intake_staff['clinic_a']}/patients/"
    helena = f"Helena Sintética {uuid4().hex[:4]}"
    _sign_in(page, renewal_base_url, intake_staff)
    _open_blank_search(page, renewal_base_url, patients, root)
    registering = _register_and_land(page, patients, helena, root)
    found = _find_across_pages(page, renewal_base_url, patients, helena, root)
    _disambiguate_same_name(page)
    _book_handoff(page, helena, root)
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "staff-intake",
            "width": width,
            "assertion": "blank search, register with the POST held (busy state),"
            " completion notice, find with the POST held (busy state), pagination,"
            " reload, same-name disambiguation, keyboard focus order, booking handoff",
            "page_size": PAGE_SIZE,
            "total": found["total"],
            "in_flight": {
                "register": registering,
                "search": found["search_in_flight"],
            },
            "console_errors": errors,
        }
    )


def test_native_post_completes_find_and_register_without_javascript(
    intake_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    intake_staff: dict[str, str],
) -> None:
    context: BrowserContext = intake_browser.new_context(
        locale="pt-BR",
        viewport={"width": 768, "height": 1024},
        java_script_enabled=False,
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    patients = f"/intake/clinics/{intake_staff['clinic_a']}/patients/"
    native = f"Nádia Sintética {uuid4().hex[:4]}"
    try:
        _sign_in(page, renewal_base_url, intake_staff)
        page.goto(f"{renewal_base_url}{patients}new/")
        page.locator("#id_full_name").fill(native)
        page.locator("#id_birth_date").fill("1984-09-09")
        with page.expect_navigation():
            page.locator(REGISTER).click()
        page.wait_for_url(f"**{patients}")
        expect(page.locator("#intake-registered")).to_be_visible()
        page.locator("#id_q").fill(native)
        with page.expect_navigation():
            page.locator(SEARCH).click()
        assert page.url == f"{renewal_base_url}{patients}"
        assert _row_names(page) == [native]
        # Without scripts the browser owns the focus move: the status is the
        # document's only autofocus candidate and is programmatically focusable.
        _assert_autofocus_target(page, "patient-results-status")
        assert _no_overflow(page)
        _capture(page, renewal_artifact_root, "native-results-768", full_page=False)

        # Native validation error: the summary is the focus target, the input kept.
        _remove_attribute(page, "#id_q", "minlength")
        page.locator("#id_q").fill("N")
        with page.expect_navigation():
            page.locator(SEARCH).click()
        _assert_autofocus_target(page, "intake-errors")
        expect(page.locator("#id_q")).to_have_value("N")
        expect(page.locator("#id_q")).to_have_attribute(
            "aria-describedby", "patient-search-help id_q_error"
        )
        _capture(page, renewal_artifact_root, "native-invalid-768", full_page=False)
    finally:
        context.close()


# --------------------------------------------------------------------------
# Failure journey steps
# --------------------------------------------------------------------------


def _empty_and_invalid_search(page: Page, patients: str, root: Path) -> None:
    width = _width(page)
    status = page.locator(STATUS)
    _search(page, "Zeferino")
    expect(status).to_have_text(
        gettext("No patient named \u201c%(term)s\u201d in this clinic.")
        % {"term": "Zeferino"}
    )
    assert _focused(page) == "patient-results-status"
    expect(page.locator(".intake-empty a.button")).to_have_attribute(
        "href", f"{patients}new/"
    )
    expect(page.locator(".intake-table")).to_have_count(0)
    assert _no_overflow(page)
    _capture(page, root, f"search-empty-{width}")

    # Invalid search data over HTMX: the alert names the field and the fix,
    # the typed value stays in the input.
    _remove_attribute(page, "#id_q", "minlength")
    _search(page, "Z")
    alert = page.locator(ALERT)
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text(gettext("We could not run that search"))
    expect(alert).to_contain_text(gettext("Patient name"))
    assert _focused(page) == "intake-errors"
    expect(page.locator("#id_q")).to_have_value("Z")
    assert _no_overflow(page)
    _capture(page, root, f"search-invalid-{width}")


def _long_name(page: Page, root: Path) -> None:
    """An 80-character name stays whole at this width; nothing scrolls sideways."""
    _search(page, "Albuquerque")
    assert _row_names(page) == [LONG_NAME]
    expect(page.locator(".intake-table tbody th")).to_have_text(LONG_NAME)
    assert _no_overflow(page)
    _capture(page, root, f"search-long-name-{_width(page)}")


def _invalid_registration(page: Page, base_url: str, patients: str, root: Path) -> None:
    page.goto(f"{base_url}{patients}new/")
    page.locator("#id_full_name").fill("Teste Sintético Futuro")
    page.locator("#id_birth_date").fill("3999-01-01")
    assert _submit(page, REGISTER) == OK
    alert = page.locator(ALERT)
    expect(alert).to_contain_text(gettext("We could not register that patient"))
    expect(alert).to_contain_text(
        gettext(
            "Check the registration fields: the birth date cannot be in the "
            "future and the document number must be valid."
        )
    )
    assert _focused(page) == "intake-errors"
    expect(page.locator("#id_full_name")).to_have_value("Teste Sintético Futuro")
    expect(page.locator("#id_birth_date")).to_have_value("3999-01-01")
    assert _no_overflow(page)
    _capture(page, root, f"register-invalid-{_width(page)}")
    # No name is natively required: a legal or a social name is enough,
    # and the server names the missing identity when both are blank.
    expect(page.locator("#id_full_name")).not_to_have_attribute("required", "")
    page.locator("#id_full_name").fill("   ")
    page.locator("#id_birth_date").fill("1990-01-01")
    assert _submit(page, REGISTER) == OK
    expect(page.locator("#id_full_name")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#id_full_name_error")).to_be_visible()
    assert _focused(page) == "intake-errors"


def _duplicate_registration(
    page: Page, base_url: str, patients: str, root: Path
) -> None:
    """One key with the same data registers once; other data is refused."""
    key = page.locator("input[name=idempotency_key]").get_attribute("value")
    assert key
    duplicate = f"Dupla Sintética {uuid4().hex[:4]}"
    payload: dict[str, str | float | bool] = {
        "csrfmiddlewaretoken": _csrf(page),
        "full_name": duplicate,
        "birth_date": "1991-06-07",
        "idempotency_key": key,
    }
    replays = [
        page.request.post(
            f"{base_url}{patients}new/",
            form=payload,
            headers={"Referer": f"{base_url}{patients}new/"},
            max_redirects=0,
        )
        for _ in range(2)
    ]
    assert [reply.status for reply in replays] == [SEE_OTHER, SEE_OTHER]
    assert all(reply.headers["location"] == patients for reply in replays)
    page.locator("#id_full_name").fill(f"{duplicate} Alterada")
    page.locator("#id_birth_date").fill("1991-06-07")
    assert _submit(page, REGISTER) == OK
    expect(page.locator(ALERT)).to_contain_text(
        gettext("This registration was already submitted differently.")
    )
    expect(page.locator("#id_full_name")).to_have_value(f"{duplicate} Alterada")
    assert _no_overflow(page)
    _capture(page, root, f"register-duplicate-{_width(page)}")
    page.goto(f"{base_url}{patients}")
    _search(page, duplicate)
    assert _row_names(page) == [duplicate]


def _foreign_clinic(
    page: Page, base_url: str, clinic_b: str, patients_a: str, root: Path
) -> None:
    """No role in clinic B: its registry is refused on GET and POST, never named."""
    patients_b = f"/intake/clinics/{clinic_b}/patients/"
    response = page.goto(f"{base_url}{patients_b}")
    assert response is not None
    assert response.status == NOT_FOUND
    expect(page.locator("h1")).to_have_text(gettext("Page unavailable"))
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    body = page.content()
    assert CLINIC_B not in body
    assert FOREIGN_NAME not in body
    assert _no_overflow(page)
    _capture(page, root, f"foreign-clinic-denied-{_width(page)}")
    page.goto(f"{base_url}{patients_a}")
    refused = page.request.post(
        f"{base_url}{patients_b}",
        form={"csrfmiddlewaretoken": _csrf(page), "q": "Marina", "page": "1"},
        headers={"Referer": f"{base_url}{patients_a}"},
    )
    assert refused.status == NOT_FOUND
    assert FOREIGN_NAME not in refused.text()
    # The same name searched in clinic A never surfaces clinic B's patient.
    _search(page, "Estrangeira")
    expect(page.locator(".intake-table")).to_have_count(0)
    assert FOREIGN_NAME not in page.content()


def test_failures_preserve_input_and_never_cross_the_clinic_scope(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    intake_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    errors = _watch_errors(page)
    root = renewal_artifact_root
    patients_a = f"/intake/clinics/{intake_staff['clinic_a']}/patients/"
    _sign_in(page, renewal_base_url, intake_staff)
    page.goto(f"{renewal_base_url}{patients_a}")
    _empty_and_invalid_search(page, patients_a, root)
    _long_name(page, root)
    _invalid_registration(page, renewal_base_url, patients_a, root)
    _duplicate_registration(page, renewal_base_url, patients_a, root)
    _foreign_clinic(page, renewal_base_url, intake_staff["clinic_b"], patients_a, root)
    # The only console entry is the refused clinic-B document itself.
    assert len(errors) == 1, errors
    assert re.search(r"\b404\b", errors[0])
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "staff-intake",
            "width": width,
            "assertion": "empty result, invalid search, long name, invalid and"
            " duplicate registration, foreign clinic denied; no horizontal"
            " overflow in any of these states",
            "console_errors": errors,
        }
    )


# --------------------------------------------------------------------------
# Reflow and preferences
# --------------------------------------------------------------------------


def _reflow(page: Page, root: Path) -> dict[str, object]:
    _search(page, "Marina")
    displays: dict[int, str] = {}
    heights: dict[int, float] = {}
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        displays[width] = str(
            page.evaluate(
                "getComputedStyle(document.querySelector('.intake-table tbody tr'))"
                ".display"
            )
        )
        heights[width] = float(
            page.evaluate(
                "document.querySelector('.intake-table tbody button')"
                ".getBoundingClientRect().height"
            )
        )
        assert _no_overflow(page), width
        assert heights[width] >= MIN_TARGET_PX, (width, heights[width])
        _capture(page, root, f"search-results-{width}", full_page=False)
    assert displays[1280] == displays[768] == "table-row"
    assert displays[640] == displays[375] == displays[320] == "block"

    # Long content at every width, captured at the 320px reflow floor: the
    # whole 80-character name stays readable and an unbreakable surname wraps
    # inside its block instead of scrolling the page.
    _search(page, "Albuquerque")
    assert _row_names(page) == [LONG_NAME]
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        assert _no_overflow(page), width
        expect(page.locator(".intake-table tbody th")).to_have_text(LONG_NAME)
    _capture(page, root, "search-long-name-320")
    _search(page, "Wolfeschlegel")
    assert _row_names(page) == [UNBROKEN_NAME]
    assert _no_overflow(page)
    _capture(page, root, "search-unbroken-name-320", full_page=False)
    report: dict[str, object] = {
        str(width): {"row_display": displays[width], "button_height": heights[width]}
        for width in WIDTHS
    }
    report["long_name_widths_without_overflow"] = list(WIDTHS)
    return report


def _forced_colors(page: Page, root: Path) -> dict[str, object]:
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    _search(page, "Zeferino")
    empty_border = page.evaluate(
        "getComputedStyle(document.querySelector('.intake-empty')).borderTopStyle"
    )
    assert empty_border == "dashed"
    _search(page, "Marina")
    row_border = page.evaluate(
        "getComputedStyle(document.querySelector('.intake-table tbody tr th'))"
        ".borderBottomStyle"
    )
    assert row_border == "solid"
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    ring = _ring(page)
    assert ring["style"] == "solid"
    _capture(page, root, "search-results-forced-colors-1280", full_page=False)
    return {"empty_border": empty_border, "row_border": row_border, "ring": ring}


def _zoom_200(
    page: Page, base_url: str, patients: str, root: Path
) -> dict[str, object]:
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
    captures = [_capture(page, root, "search-blank-zoom-200")]
    _search(page, "Albuquerque")
    expect(page.locator(".intake-table tbody th")).to_have_text(LONG_NAME)
    assert _no_overflow(page)
    row_display = str(
        page.evaluate(
            "getComputedStyle(document.querySelector('.intake-table tbody tr')).display"
        )
    )
    assert row_display == "block"
    height = float(
        page.evaluate(
            "document.querySelector('.intake-table tbody button')"
            ".getBoundingClientRect().height"
        )
    )
    assert height >= MIN_TARGET_PX, height
    captures.append(_capture(page, root, "search-results-zoom-200"))
    page.goto(f"{base_url}{patients}new/")
    page.locator("#id_full_name").fill("Teste Sintético Ampliado")
    page.locator("#id_birth_date").fill("3999-01-01")
    assert _submit(page, REGISTER) == OK
    expect(page.locator(ALERT)).to_be_visible()
    assert _focused(page) == "intake-errors"
    assert _no_overflow(page)
    captures.append(_capture(page, root, "register-invalid-zoom-200"))
    return {
        **metrics,
        "window_width": ZOOM_WINDOW,
        "row_display": row_display,
        "button_height": height,
        "captures": captures,
    }


def test_reflow_forced_colors_reduced_motion_and_zoom_keep_the_registry_usable(
    intake_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    intake_staff: dict[str, str],
) -> None:
    root = renewal_artifact_root
    patients = f"/intake/clinics/{intake_staff['clinic_a']}/patients/"
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
    for scene, options in scenes:
        context = intake_browser.new_context(locale="pt-BR", **options)
        page = context.new_page()
        page.set_default_timeout(20_000)
        try:
            _sign_in(page, renewal_base_url, intake_staff)
            page.goto(f"{renewal_base_url}{patients}")
            if scene == "widths":
                report[scene] = _reflow(page, root)
            elif scene == "forced_colors":
                report[scene] = _forced_colors(page, root)
            elif scene == "zoom_200":
                report[scene] = _zoom_200(page, renewal_base_url, patients, root)
            else:
                transition = page.evaluate(
                    "getComputedStyle(document.querySelector("
                    "'#patient-search-form button')).transitionDuration"
                )
                assert transition == "0s"
                report[scene] = {"button_transition": transition}
        finally:
            context.close()
    destination = root / "staff-intake" / "accessibility-report.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)


# --------------------------------------------------------------------------
# Demographics and exact-identifier journey (todo 17)
# --------------------------------------------------------------------------

DEMOGRAPHICS_PANEL = "#patient-demographics-panel"
DEMOGRAPHICS_FORM = "#demographics-form"
HISTORY_REGION = "#patient-correction-history [role=region]"
DEMOGRAPHICS_WIDTHS = (1280, 768, 375, 320)
AXE_URL = "/static/vendor/axe/axe.min.js"
AXE_RUN_JS = """async () => {
  const result = await axe.run(document, {
    runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
      'wcag22aa', 'best-practice']},
    resultTypes: ['violations'],
  });
  return result.violations.map((v) => ({
    id: v.id, impact: v.impact,
    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
  }));
}"""


def _cpf_for(seed: str) -> str:
    """Return a mod-11-valid CPF built from nine arbitrary digits."""
    assert len(seed) == 9
    assert seed.isdigit()
    first = sum(int(seed[i]) * (10 - i) for i in range(9)) % 11
    d1 = 0 if first < 2 else 11 - first
    second = (sum(int(seed[i]) * (11 - i) for i in range(9)) + d1 * 2) % 11
    d2 = 0 if second < 2 else 11 - second
    return f"{seed[:3]}.{seed[3:6]}.{seed[6:9]}-{d1}{d2}"


@pytest.fixture(
    params=DEMOGRAPHICS_WIDTHS, ids=[f"{width}px" for width in DEMOGRAPHICS_WIDTHS]
)
def demographics_page(
    request: pytest.FixtureRequest, intake_browser: Browser
) -> Iterator[Page]:
    """One pt-BR context per width, 320px included for the owned surface."""
    context = intake_browser.new_context(
        locale="pt-BR", viewport={"width": int(request.param), "height": 900}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    yield page
    context.close()


def _register_with_document(  # noqa: PLR0913 - the journey needs every field
    page: Page,
    base_url: str,
    patients: str,
    *,
    name: str,
    social_name: str,
    cpf: str,
) -> None:
    """Register one patient with a social name and CPF through the form."""
    page.goto(f"{base_url}{patients}new/")
    page.locator("#id_full_name").fill(name)
    page.locator("#id_birth_date").fill("1990-05-17")
    page.locator("#id_social_name").fill(social_name)
    page.locator("#id_identifier_kind").select_option("cpf")
    page.locator("#id_identifier_value").fill(cpf)
    with page.expect_navigation():
        page.locator(REGISTER).click()
    page.wait_for_url(f"**{patients}")


def _identifier_search(page: Page, *, kind: str, value: str) -> None:
    """Run one exact identifier lookup through the search form."""
    page.locator("#id_q").fill("")
    page.locator("#id_identifier_kind").select_option(kind)
    page.locator("#id_identifier_value").fill(value)
    _submit(page, SEARCH)


def _open_demographics(page: Page) -> None:
    """Post the row's Demographics action: enrollment stays in the body."""
    with page.expect_navigation():
        page.locator(
            ".intake-table tbody .button--quiet",
            has_text=gettext("Demographics"),
        ).first.click()


def _post_section(page: Page, button: str) -> None:
    """Submit one section form by its visible button and wait for the page."""
    with page.expect_navigation():
        page.get_by_role("button", name=button, exact=True).first.click()
    expect(page.locator(DEMOGRAPHICS_PANEL)).to_contain_text(gettext("Record saved."))


def _axe_violations(page: Page, base_url: str) -> list[dict[str, object]]:
    with page.expect_response(f"{base_url}{AXE_URL}") as axe_response:
        page.add_script_tag(url=f"{base_url}{AXE_URL}")
    assert axe_response.value.status == OK
    violations = page.evaluate(AXE_RUN_JS)
    assert isinstance(violations, list)
    return violations


def _keyboard_scrolls(page: Page, selector: str) -> bool:
    """Focus one scroll region by keyboard and scroll it with ArrowRight."""
    region = page.locator(selector)
    overflows = region.evaluate("(el) => el.scrollWidth > el.clientWidth")
    if not overflows:
        return True
    region.focus()
    assert region.evaluate("(el) => el === document.activeElement")
    page.keyboard.press("ArrowRight")
    page.wait_for_function(
        "(selector) => document.querySelector(selector).scrollLeft > 0", arg=selector
    )
    return True


def test_demographics_edit_surface_search_and_duplicate_review(  # noqa: PLR0915 - one end-to-end journey
    demographics_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    intake_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    """Receptionist registers a social name and CPF, then corrects identity,
    records a deliberate non-answer, an address, an emergency contact and a
    health plan through the real forms; exact search shows the corrected
    name; the history is translated and keyboard-scrollable; axe reports no
    violation; a second registration with the same CPF warns."""
    page = demographics_page
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    patients = f"/intake/clinics/{intake_staff['clinic_a']}/patients/"
    # Width-bound synthetic identity: distinct per matrix width in the shared
    # session registry, never a repeated-digit CPF.
    legal = f"Aurora Sintética L{width}"
    social = f"Rosa Sintética L{width}"
    cpf = _cpf_for(f"{width:04d}17017")

    _sign_in(page, renewal_base_url, intake_staff)
    _register_with_document(
        page, renewal_base_url, patients, name=legal, social_name=social, cpf=cpf
    )
    expect(page.locator("#intake-registered")).to_be_visible()
    _capture(page, root, f"demographics-registered-{width}")

    # The banner column shows the social name first, with the legal name kept.
    _search(page, social)
    first_row = page.locator(".intake-table tbody th").first
    expect(first_row).to_contain_text(social)
    expect(first_row).to_contain_text(legal)
    assert _no_overflow(page), _overflow_offenders(page)
    _capture(page, root, f"demographics-search-social-{width}")

    # Exact CPF lookup lands on the same patient.
    _identifier_search(page, kind="cpf", value=cpf)
    expect(page.locator(STATUS)).to_have_text(_status_text(1, 1, 1))
    expect(page.locator(".intake-table tbody th").first).to_contain_text(social)
    assert _no_overflow(page), _overflow_offenders(page)
    _capture(page, root, f"demographics-identifier-hit-{width}")

    # Open the profile: values travel in POST bodies, never the URL.
    _open_demographics(page)
    assert "enrollment" not in page.url
    expect(page.locator("h1")).to_have_text(gettext("Patient demographics"))
    expect(page.locator(DEMOGRAPHICS_PANEL)).to_contain_text(social)
    expect(page.locator(DEMOGRAPHICS_PANEL)).to_contain_text(legal)
    document_cell = page.locator("#patient-documents tbody td").first
    expect(document_cell).to_contain_text(re.sub(r"\D", "", cpf)[-4:])
    assert cpf not in page.content()
    # Coded answers render translated labels; stored codes stay invisible.
    gender = page.locator(
        "#id_gender_identity option", has_text=gettext("Declined to answer")
    )
    expect(gender).to_have_attribute("value", "declined")
    _capture(page, root, f"demographics-profile-{width}")
    assert _no_overflow(page), _overflow_offenders(page)

    # One correction: a new social name and a deliberate non-answer.
    page.locator("#id_social_name").fill(f"{social} Correção")
    page.locator("#id_gender_identity").select_option("declined")
    page.locator("#id_pronouns_status").select_option("not_informed")
    page.locator("#id_reason").fill("paciente pediu correção")
    with page.expect_navigation():
        page.locator(f"{DEMOGRAPHICS_FORM} button[type=submit]").click()
    expect(page.locator(DEMOGRAPHICS_PANEL)).to_contain_text(
        gettext("Demographics saved.")
    )
    expect(page.locator("#id_expected_version")).to_have_value("2")
    expect(page.locator("#id_gender_identity")).to_have_value("declined")
    expect(page.locator("#id_pronouns_status")).to_have_value("not_informed")

    # Address, emergency contact and health plan through their own forms.
    page.locator("#address-new-postal_code").fill("01310-100")
    page.locator("#address-new-city").fill("São Paulo")
    page.locator("#address-new-state_code").select_option("SP")
    _post_section(page, gettext("Add address"))
    page.locator("#address-home-street").fill("Av. Paulista")
    _post_section(page, f"{gettext('Save address')} {gettext('Home')}")
    expect(page.locator("#address-home-street")).to_have_value("Av. Paulista")
    page.locator("#contact-new-name").fill("Contato Sintético")
    page.locator("#contact-new-phone").fill("(11) 98888-7777")
    _post_section(page, gettext("Add contact"))
    expect(page.locator("#contact-1-name")).to_have_value("Contato Sintético")
    page.locator("#membership-new-payer_name").fill("Operadora Sintética")
    page.locator("#membership-new-membership_number").fill("0000-SINT")
    _post_section(page, gettext("Add health plan"))
    expect(page.locator("#membership-1-payer_name")).to_have_value(
        "Operadora Sintética"
    )

    # The history names fields in pt-BR and scrolls by keyboard.
    history = page.locator("#patient-correction-history")
    expect(history).to_contain_text("paciente pediu correção")
    expect(history).to_contain_text(str(gettext("Social name")))
    assert "social_name" not in history.inner_text()
    assert _keyboard_scrolls(page, HISTORY_REGION)
    assert _no_overflow(page), _overflow_offenders(page)
    violations = _axe_violations(page, renewal_base_url)
    assert violations == [], violations
    _capture(page, root, f"demographics-corrected-{width}")

    # Exact search after corrections shows the current social name.
    page.goto(f"{renewal_base_url}{patients}")
    _identifier_search(page, kind="cpf", value=cpf)
    expect(page.locator(".intake-table tbody th").first).to_contain_text(
        f"{social} Correção"
    )

    # The second registration with the same CPF saves and warns, never blocks.
    other = f"Íris Sintética L{width}"
    _register_with_document(
        page, renewal_base_url, patients, name=other, social_name="", cpf=cpf
    )
    expect(page.locator("#intake-possible-duplicate")).to_be_visible()
    expect(page.locator("#intake-possible-duplicate")).to_contain_text(
        gettext(
            "Another registration already uses this document. "
            "A reviewer will confirm whether they are the same patient."
        ).split(".")[0]
    )
    assert _no_overflow(page), _overflow_offenders(page)
    _capture(page, root, f"demographics-duplicate-warning-{width}")

    # Both patients answer the same exact lookup.
    _identifier_search(page, kind="cpf", value=cpf)
    expect(page.locator(STATUS)).to_have_text(_status_text(2, 1, 1))
    assert _no_overflow(page), _overflow_offenders(page)
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "staff-intake",
            "width": width,
            "assertion": "register with social name and CPF; correct identity "
            "with a declined answer; add/edit address, emergency contact and "
            "health plan; exact search shows the corrected name; translated, "
            "keyboard-scrollable history; axe 0 violations; duplicate-CPF warning",
            "axe_violations": violations,
            "console_errors": errors,
        }
    )
