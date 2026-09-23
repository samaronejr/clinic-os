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
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext, ngettext
from playwright.sync_api import expect

from renewal.browser._protected import encrypt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
    page.wait_for_function(SETTLED_JS)
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
    page.wait_for_function(SETTLED_JS)
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
    page.wait_for_function(SETTLED_JS)
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
        gettext("Enter a patient name and a valid birth date.")
    )
    assert _focused(page) == "intake-errors"
    expect(page.locator("#id_full_name")).to_have_value("Teste Sintético Futuro")
    expect(page.locator("#id_birth_date")).to_have_value("3999-01-01")
    assert _no_overflow(page)
    _capture(page, root, f"register-invalid-{_width(page)}")
    _remove_attribute(page, "#id_full_name", "required")
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
