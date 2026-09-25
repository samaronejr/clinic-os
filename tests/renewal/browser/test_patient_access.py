"""Real-browser patient-access journey, served as ``clinic_app``.

Owner access is confined to synthetic staff, patient and expired-grant
setup. Every wait subscribes to a navigation, a response or a DOM state,
never a timer. Captures contain synthetic names only; the invitation code
appears once in the staff response body and never in a URL.

Evidence grid: the journey test runs once per matrix width (1280, 768 and
375) and names every capture ``<state>-<width>``; the failure test adds
the denied, consumed, expired, wrong-clinic, forged-context and revoked
states at the same widths. The keyboard test drives issue, redemption and
sign-out without a pointer, and the matrix test adds the 320px reflow
floor, forced colors, reduced motion, a 1280px window at 200% zoom and
long-content wrapping; every scene watches the patient pages' console as
well as the staff pages'.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._page_wait import wait_for_js
from renewal.browser._protected import encrypt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Locator, Page

CLINIC_A = "Clínica Vila Mariana"
CLINIC_B = "Clínica Vila Olímpia"
PATIENT_A = "Marina Sintética Portal"
PATIENT_B = "Bia Sintética Portal"
EXPIRED_PATIENT = "Célia Sintética Expirado"
LONG_PATIENT = (
    "Maria Aparecida Wolfeschlegelsteinhausenbergerdorff Conceição dos "
    "Santos Oliveira Ferreira de Albuquerque Sintética Portal"
)
MATRIX_WIDTHS = (1280, 768, 375)
WIDTHS = (1280, 768, 640, 375, 320)
ZOOM_WINDOW = 1280  # physical window width behind the 200% zoom scene
ZOOM_FACTOR = 2
MIN_TARGET_PX = 44
OK = 200
FORBIDDEN = 403
FOUND = 302
SETTLED_JS = (
    "!document.querySelector('.htmx-request, .htmx-settling, .htmx-added,"
    ' [aria-busy="true"]\')'
)
INVALID_CODE = gettext(
    "This access code is not valid. Check the code or ask the clinic for a "
    "new invitation."
)


@pytest.fixture(scope="session")
def access_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed a clinic-A receptionist, enrolled patients and an expired grant."""
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    values = {
        "dsn": os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"],
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "clinic_b": str(uuid4()),
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "receptionist": f"recepcao-{uuid4().hex[:8]}",
        "receptionist_id": str(uuid4()),
        "password": secrets.token_urlsafe(24),
        "expired_code": secrets.token_urlsafe(32),
    }
    registry = [
        (values["clinic_a"], PATIENT_A, "1990-05-17"),
        (values["clinic_a"], EXPIRED_PATIENT, "1992-11-23"),
        (values["clinic_a"], LONG_PATIENT, "1979-08-14"),
        (values["clinic_b"], PATIENT_B, "1988-03-12"),
    ]
    enrollments: dict[str, str] = {}
    patients: dict[str, str] = {}
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
                f"{values['receptionist']}@access.invalid",
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
            enrollment_id = str(uuid4())
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
                    enrollment_id,
                    values["organization"],
                    clinic_id,
                    patient_id,
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
            enrollments[name] = enrollment_id
            patients[name] = patient_id
        # One expired invitation for the expired patient: only the hash is
        # stored, exactly as the service writes it.
        connection.execute(
            "INSERT INTO clinic_app.intake_patientaccessgrant "
            "(id, organization_id, clinic_id, patient_id, enrollment_id, "
            "issued_by_id, issued_by_label, secret_hash, operations, "
            "expires_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
            "ARRAY['enrollment_view']::varchar(32)[], %s, %s)",
            [
                str(uuid4()),
                values["organization"],
                values["clinic_a"],
                patients[EXPIRED_PATIENT],
                enrollments[EXPIRED_PATIENT],
                values["receptionist_id"],
                values["receptionist"],
                hashlib.sha256(values["expired_code"].encode()).digest(),
                datetime.now(UTC) - timedelta(hours=1),
                datetime.now(UTC) - timedelta(hours=25),
            ],
        )
    values["enrollment_a"] = enrollments[PATIENT_A]
    values["enrollment_b"] = enrollments[PATIENT_B]
    values["enrollment_expired"] = enrollments[EXPIRED_PATIENT]
    values["enrollment_long"] = enrollments[LONG_PATIENT]
    values["patient_b"] = patients[PATIENT_B]
    return values


@pytest.fixture
def access_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


@pytest.fixture(params=MATRIX_WIDTHS, ids=[f"{width}px" for width in MATRIX_WIDTHS])
def journey(request: pytest.FixtureRequest, access_browser: Browser) -> Iterator[Page]:
    """One pt-BR context per matrix width; tests read the width back."""
    context = access_browser.new_context(
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


def _watch_errors(page: Page, *, allowed_denials: tuple[str, ...] = ()) -> list[str]:
    """Collect page errors and unexpected console errors.

    Chromium logs a denied document load as a console error; a path in
    ``allowed_denials`` is a deliberate denial the test itself asserts, not
    a defect. Everything else is recorded verbatim.
    """
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def _console(message: object) -> None:
        if getattr(message, "type", None) != "error":
            return
        text = str(getattr(message, "text", ""))
        location = getattr(message, "location", None) or {}
        path = urlsplit(str(location.get("url", ""))).path
        if "403" in text and path in allowed_denials:
            return
        errors.append(text)

    page.on("console", _console)
    return errors


def _capture(page: Page, root: Path, name: str, *, full_page: bool = True) -> str:
    destination = root / "patient-access" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=full_page)
    destination.chmod(0o600)
    return destination.name


def _no_overflow(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))


def _overflowing(page: Page) -> list[str]:
    """List the elements wider than the viewport for failure diagnostics."""
    return list(
        page.evaluate(
            "Array.from(document.querySelectorAll('body *'))"
            ".filter((element) => element.getBoundingClientRect().right >"
            " document.documentElement.clientWidth + 1)"
            ".map((element) => element.tagName + '.' + element.className"
            " + ' right=' + Math.round(element.getBoundingClientRect().right))"
            ".slice(0, 10)"
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


def _sign_in(page: Page, base_url: str, staff: dict[str, str]) -> None:
    page.goto(f"{base_url}/auth/login/")
    page.locator("#id_username").fill(staff["receptionist"])
    page.locator("#id_password").fill(staff["password"])
    with page.expect_navigation():
        page.locator("button[type=submit]").click()
    page.wait_for_url("**/auth/protected/")


def _open_access(page: Page, base_url: str, staff: dict[str, str], name: str) -> None:
    """Search the patient and open the access screen through the row action."""
    patients = f"/intake/clinics/{staff['clinic_a']}/patients/"
    page.goto(f"{base_url}{patients}")
    page.locator("#id_q").fill(name)
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as received:
        page.locator("#patient-search-form button[type=submit]").click()
    assert received.value.status == OK
    wait_for_js(page, SETTLED_JS)
    row = page.locator(".intake-table tbody tr", has_text=name)
    with page.expect_navigation():
        row.locator("button", has_text=gettext("Access")).click()
    expect(page.locator("h1")).to_have_text(gettext("Patient access"))


def _issue_code(page: Page) -> str:
    """Issue one invitation through the staff screen and read the code."""
    with page.expect_navigation():
        page.locator("button", has_text=gettext("Issue a new invitation")).click()
    code = page.locator("#issued-code").inner_text()
    assert code
    return code


def _redeem(page: Page, base_url: str, clinic_id: str, code: str) -> None:
    """Submit one code through the real redemption form."""
    page.goto(f"{base_url}/patient/access/{clinic_id}/")
    page.locator("#id_code").fill(code)
    with page.expect_navigation():
        page.locator("button", has_text=gettext("Continue")).click()


def _patient_journey(  # noqa: PLR0913 - the journey needs its full context
    patient: Page,
    base_url: str,
    staff: dict[str, str],
    code: str,
    width: int,
    root: Path,
) -> None:
    """Redeem, view the bound enrollment and sign out in the patient context."""
    with patient.expect_response(
        lambda response: (
            response.request.method == "POST" and "/patient/access/" in response.url
        )
    ) as received:
        _redeem(patient, base_url, staff["clinic_a"], code)
    assert received.value.status == FOUND
    patient.wait_for_url("**/patient/")
    assert code not in patient.url
    expect(patient.locator("h1")).to_have_text(gettext("Your enrollment"))
    expect(patient.locator("#patient-name")).to_have_text(PATIENT_A)
    expect(patient.locator("#patient-clinic")).to_have_text(CLINIC_A)
    body = patient.content()
    assert PATIENT_B not in body
    assert EXPIRED_PATIENT not in body
    assert code not in body
    assert _no_overflow(patient)
    _capture(patient, root, f"patient-home-{width}")

    # Sign out ends the session server-side and shows the gate.
    with patient.expect_navigation():
        patient.locator("button", has_text=gettext("Sign out")).click()
    expect(patient.locator("h1")).to_have_text(gettext("You are signed out"))
    _capture(patient, root, f"patient-signed-out-{width}")

    denied = patient.goto(f"{base_url}/patient/")
    assert denied is not None
    assert denied.status == FORBIDDEN
    expect(patient.locator("h1")).to_have_text(gettext("Access required"))
    _capture(patient, root, f"patient-gate-{width}")


def test_staff_issues_and_patient_redeems_views_and_signs_out(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    access_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    _sign_in(page, renewal_base_url, access_staff)
    _open_access(page, renewal_base_url, access_staff, PATIENT_A)
    assert _no_overflow(page)
    _capture(page, root, f"access-manage-{width}")

    code = _issue_code(page)
    expect(page.locator(".access-issued")).to_contain_text(gettext("Invitation code"))
    expect(page.locator(".contacts-table").first).to_contain_text(gettext("Active"))
    assert code not in page.url
    assert _no_overflow(page)
    _capture(page, root, f"access-issued-{width}")

    # The patient redeems the code in a separate context: POST body only.
    browser = page.context.browser
    assert browser is not None
    patient_context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    patient = patient_context.new_page()
    patient.set_default_timeout(20_000)
    # The journey deliberately loads the denied gate after sign-out.
    patient_errors = _watch_errors(patient, allowed_denials=("/patient/",))
    try:
        _patient_journey(patient, renewal_base_url, access_staff, code, width, root)
    finally:
        patient_context.close()
    assert not patient_errors, patient_errors

    # The staff manage screen now shows the consumed invitation and the
    # revoked session.
    _open_access(page, renewal_base_url, access_staff, PATIENT_A)
    expect(page.locator(".contacts-table").first).to_contain_text(gettext("Used"))
    expect(page.locator(".contacts-table").nth(1)).to_contain_text(gettext("Revoked"))
    _capture(page, root, f"access-after-logout-{width}")

    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "patient-access",
            "width": width,
            "assertion": "staff issue shows the code once, redemption POSTs"
            " the code, the patient sees only the bound enrollment, sign-out"
            " revokes the session and the gate denies further access",
            "console_errors": errors,
            "patient_console_errors": patient_errors,
        }
    )


def _forged_context_binds_the_grant(  # noqa: PLR0913 - full scene context
    patient: Page,
    base_url: str,
    staff: dict[str, str],
    code: str,
    width: int,
    root: Path,
) -> None:
    """Post forged context fields; the session still binds the grant."""
    patient.goto(f"{base_url}/patient/access/{staff['clinic_a']}/")
    token = patient.locator("input[name=csrfmiddlewaretoken]").get_attribute("value")
    assert token
    forged = patient.request.post(
        f"{base_url}/patient/access/{staff['clinic_a']}/",
        max_redirects=0,
        form={
            "csrfmiddlewaretoken": token,
            "code": code,
            "organization_id": str(uuid4()),
            "patient_id": staff["patient_b"],
            "enrollment_id": staff["enrollment_b"],
        },
    )
    assert forged.status == FOUND
    home = patient.goto(f"{base_url}/patient/")
    assert home is not None
    assert home.status == OK
    expect(patient.locator("#patient-name")).to_have_text(PATIENT_A)
    expect(patient.locator("#patient-clinic")).to_have_text(CLINIC_A)
    assert PATIENT_B not in patient.content()
    _capture(patient, root, f"patient-bound-enrollment-{width}")

    # The patient session cannot open staff surfaces.
    staff_denied = patient.goto(
        f"{base_url}/intake/clinics/{staff['clinic_a']}/patients/"
    )
    assert staff_denied is not None
    assert staff_denied.status == FORBIDDEN
    _capture(patient, root, f"patient-staff-denied-{width}")


def test_failure_states_reject_and_stay_generic(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    access_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    _sign_in(page, renewal_base_url, access_staff)
    _open_access(page, renewal_base_url, access_staff, PATIENT_A)
    code = _issue_code(page)

    browser = page.context.browser
    assert browser is not None
    patient_context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    patient = patient_context.new_page()
    patient.set_default_timeout(20_000)
    # The journey deliberately loads the revoked gate and a staff surface.
    patient_errors = _watch_errors(
        patient,
        allowed_denials=(
            "/patient/",
            f"/intake/clinics/{access_staff['clinic_a']}/patients/",
        ),
    )
    try:
        # Unknown, wrong-clinic and expired codes fail identically.
        _redeem(patient, renewal_base_url, access_staff["clinic_a"], "bogus")
        alert = patient.locator("#access-errors")
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text(INVALID_CODE)
        _capture(patient, root, f"redeem-unknown-{width}")

        _redeem(patient, renewal_base_url, access_staff["clinic_b"], code)
        expect(patient.locator("#access-errors")).to_contain_text(INVALID_CODE)
        _capture(patient, root, f"redeem-wrong-clinic-{width}")

        _redeem(
            patient,
            renewal_base_url,
            access_staff["clinic_a"],
            access_staff["expired_code"],
        )
        expect(patient.locator("#access-errors")).to_contain_text(INVALID_CODE)
        _capture(patient, root, f"redeem-expired-{width}")

        _forged_context_binds_the_grant(
            patient, renewal_base_url, access_staff, code, width, root
        )

        # Staff revocation ends the live session immediately.
        _open_access(page, renewal_base_url, access_staff, PATIENT_A)
        with page.expect_navigation():
            page.locator("button", has_text=gettext("Revoke access")).first.click()
        expect(page.locator(".feedback--success")).to_contain_text(
            gettext(
                "Patient access revoked. The invitation and its sessions no "
                "longer work."
            )
        )
        _capture(page, root, f"access-revoked-{width}")

        revoked = patient.goto(f"{renewal_base_url}/patient/")
        assert revoked is not None
        assert revoked.status == FORBIDDEN
        expect(patient.locator("h1")).to_have_text(gettext("Access required"))
        _capture(patient, root, f"patient-revoked-{width}")

        # The consumed code cannot mint another session.
        _redeem(patient, renewal_base_url, access_staff["clinic_a"], code)
        expect(patient.locator("#access-errors")).to_contain_text(INVALID_CODE)
        _capture(patient, root, f"redeem-consumed-{width}")
    finally:
        patient_context.close()

    # No console errors on either side: even the refused documents render
    # cleanly.
    assert not errors, errors
    assert not patient_errors, patient_errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "patient-access",
            "width": width,
            "assertion": "unknown, wrong-clinic, expired and consumed codes"
            " fail with the identical non-enumerating alert; forged context"
            " fields are ignored; staff revocation ends the live session;"
            " patient sessions cannot open staff surfaces",
            "console_errors": errors,
            "patient_console_errors": patient_errors,
        }
    )


def test_native_post_completes_the_journey_without_javascript(
    access_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    access_staff: dict[str, str],
) -> None:
    context = access_browser.new_context(
        locale="pt-BR",
        viewport={"width": 768, "height": 1024},
        java_script_enabled=False,
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    try:
        _sign_in(page, renewal_base_url, access_staff)
        _open_access(page, renewal_base_url, access_staff, PATIENT_A)
        code = _issue_code(page)

        patient_context = access_browser.new_context(
            locale="pt-BR",
            viewport={"width": 768, "height": 1024},
            java_script_enabled=False,
        )
        patient = patient_context.new_page()
        patient.set_default_timeout(20_000)
        try:
            _redeem(patient, renewal_base_url, access_staff["clinic_a"], code)
            patient.wait_for_url("**/patient/")
            expect(patient.locator("#patient-name")).to_have_text(PATIENT_A)
            with patient.expect_navigation():
                patient.locator("button", has_text=gettext("Sign out")).click()
            expect(patient.locator("h1")).to_have_text(gettext("You are signed out"))
            assert _no_overflow(patient)
            _capture(patient, renewal_artifact_root, "native-journey-768")
        finally:
            patient_context.close()
    finally:
        context.close()


# --------------------------------------------------------------------------
# Keyboard traversal and the accessibility matrix
# --------------------------------------------------------------------------


def _tab_until(
    page: Page,
    target: Locator,
    focused: list[str],
    *,
    limit: int = 40,
) -> None:
    """Tab forward until ``target`` holds focus; every stop keeps its ring."""
    for _ in range(limit):
        page.keyboard.press("Tab")
        ring = _ring(page)
        assert ring["style"] == "solid", (focused, ring)
        assert ring["width"] != "0px", (focused, ring)
        focused.append(
            str(
                page.evaluate(
                    "document.activeElement &&"
                    " (document.activeElement.name"
                    " || document.activeElement.tagName)"
                )
            )
        )
        if target.evaluate("(element) => element === document.activeElement"):
            return
    pytest.fail("target control never received keyboard focus")


def test_keyboard_reaches_and_activates_issue_redeem_and_sign_out(
    access_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    access_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    """Tab order reaches every action and Enter submits it on both sides."""
    context = access_browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    errors = _watch_errors(page)
    patient_errors: list[str] = []
    try:
        _sign_in(page, renewal_base_url, access_staff)
        _open_access(page, renewal_base_url, access_staff, PATIENT_A)

        # Keyboard-only issue: every tab stop shows a visible ring.
        issue_button = page.locator(
            "button", has_text=gettext("Issue a new invitation")
        )
        focused: list[str] = []
        _tab_until(page, issue_button, focused)
        with page.expect_navigation():
            page.keyboard.press("Enter")
        expect(page.locator(".access-issued")).to_contain_text(
            gettext("Invitation code")
        )
        code = page.locator("#issued-code").inner_text()
        assert code
        _capture(page, renewal_artifact_root, "keyboard-issued-1280")

        # Keyboard-only redemption in the patient context.
        patient_context = access_browser.new_context(
            locale="pt-BR", viewport={"width": 1280, "height": 900}
        )
        patient = patient_context.new_page()
        patient.set_default_timeout(20_000)
        patient_errors = _watch_errors(patient)
        try:
            patient.goto(
                f"{renewal_base_url}/patient/access/{access_staff['clinic_a']}/"
            )
            focused = []
            _tab_until(patient, patient.locator("#id_code"), focused)
            patient.keyboard.type(code)
            continue_button = patient.locator("button", has_text=gettext("Continue"))
            _tab_until(patient, continue_button, focused)
            with patient.expect_navigation():
                patient.keyboard.press("Enter")
            patient.wait_for_url("**/patient/")
            expect(patient.locator("#patient-name")).to_have_text(PATIENT_A)

            sign_out = patient.locator("button", has_text=gettext("Sign out"))
            _tab_until(patient, sign_out, focused)
            with patient.expect_navigation():
                patient.keyboard.press("Enter")
            expect(patient.locator("h1")).to_have_text(gettext("You are signed out"))
            _capture(patient, renewal_artifact_root, "keyboard-signed-out-1280")
        finally:
            patient_context.close()
    finally:
        context.close()
    assert not errors
    assert not patient_errors, patient_errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "patient-access",
            "width": 1280,
            "assertion": "keyboard traversal reaches the staff issue action,"
            " the patient code field, the continue action and sign-out with"
            " a visible focus ring at every stop; Enter submits each",
            "console_errors": errors,
            "patient_console_errors": patient_errors,
        }
    )


def _access_reflow(
    page: Page,
    patient: Page,
    root: Path,
) -> dict[str, object]:
    """The populated access screens at every width down to 320px."""
    heights: dict[int, dict[str, float]] = {}
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        patient.set_viewport_size({"width": width, "height": 900})
        staff_height = float(
            page.evaluate(
                "document.querySelector('.contacts-table tbody button')"
                ".getBoundingClientRect().height"
            )
        )
        patient_height = float(
            patient.evaluate(
                "document.querySelector('.patient-details + form button,"
                " form .button--secondary').getBoundingClientRect().height"
            )
        )
        assert _no_overflow(page), (width, _overflowing(page))
        assert _no_overflow(patient), (width, _overflowing(patient))
        assert staff_height >= MIN_TARGET_PX, (width, staff_height)
        assert patient_height >= MIN_TARGET_PX, (width, patient_height)
        heights[width] = {
            "staff_button": staff_height,
            "patient_button": patient_height,
        }
    _capture(page, root, "access-reflow-320", full_page=False)
    _capture(patient, root, "patient-home-reflow-320", full_page=False)
    return {"button_heights": heights}


def _access_forced_colors(
    page: Page,
    patient: Page,
    root: Path,
) -> dict[str, object]:
    """Borders and focus survive forced colors on both surfaces."""
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    assert patient.evaluate("matchMedia('(forced-colors: active)').matches")
    row_border = page.evaluate(
        "getComputedStyle(document.querySelector('.contacts-table tbody tr th'))"
        ".borderBottomStyle"
    )
    assert row_border == "solid"
    panel_border = patient.evaluate(
        "getComputedStyle(document.querySelector('.panel')).borderTopStyle"
    )
    assert panel_border == "solid"
    page.keyboard.press("Tab")
    staff_ring = _ring(page)
    assert staff_ring["style"] == "solid"
    patient.keyboard.press("Tab")
    patient_ring = _ring(patient)
    assert patient_ring["style"] == "solid"
    _capture(page, root, "access-forced-colors-1280", full_page=False)
    _capture(patient, root, "patient-forced-colors-1280", full_page=False)
    return {
        "row_border": row_border,
        "panel_border": panel_border,
        "staff_ring": staff_ring,
        "patient_ring": patient_ring,
    }


def _access_zoom_200(
    page: Page,
    patient: Page,
    root: Path,
) -> dict[str, object]:
    """A 1280px window at 200% browser zoom: 640 CSS px at device ratio 2.

    Chromium implements page zoom as a device-scale change, so the context's
    ``device_scale_factor`` and halved viewport are the zoomed window itself;
    every capture here is 1280 device pixels wide with 200%-size text.
    """
    metrics: dict[str, object] = {}
    for label, surface in (("staff", page), ("patient", patient)):
        values = dict(
            surface.evaluate(
                "({device_pixel_ratio: devicePixelRatio,"
                " css_viewport_width: innerWidth,"
                " root_font_size:"
                " getComputedStyle(document.documentElement).fontSize})"
            )
        )
        assert values["device_pixel_ratio"] == ZOOM_FACTOR, values
        assert values["css_viewport_width"] == ZOOM_WINDOW // ZOOM_FACTOR, values
        assert _no_overflow(surface), _overflowing(surface)
        metrics[label] = values
    staff_height = float(
        page.evaluate(
            "document.querySelector('.contacts-table tbody button')"
            ".getBoundingClientRect().height"
        )
    )
    patient_height = float(
        patient.evaluate(
            "document.querySelector('form .button--secondary')"
            ".getBoundingClientRect().height"
        )
    )
    assert staff_height >= MIN_TARGET_PX, staff_height
    assert patient_height >= MIN_TARGET_PX, patient_height
    captures = [
        _capture(page, root, "access-zoom-200"),
        _capture(patient, root, "patient-home-zoom-200"),
    ]
    return {
        **metrics,
        "window_width": ZOOM_WINDOW,
        "staff_button_height": staff_height,
        "patient_button_height": patient_height,
        "captures": captures,
    }


def _patient_scene(  # noqa: PLR0913 - the scene needs its full context
    access_browser: Browser,
    options: dict[str, Any],
    renewal_base_url: str,
    access_staff: dict[str, str],
    code: str,
    name: str,
) -> tuple[Page, list[str], BrowserContext]:
    """Open the patient context for one scene and redeem the issued code."""
    patient_context = access_browser.new_context(locale="pt-BR", **options)
    patient = patient_context.new_page()
    patient.set_default_timeout(20_000)
    patient_errors = _watch_errors(patient)
    _redeem(patient, renewal_base_url, access_staff["clinic_a"], code)
    patient.wait_for_url("**/patient/")
    expect(patient.locator("#patient-name")).to_have_text(name)
    return patient, patient_errors, patient_context


def _reduced_motion(page: Page, patient: Page, root: Path) -> dict[str, object]:
    """Both surfaces drop every transition under reduced motion."""
    transition = page.evaluate(
        "getComputedStyle(document.querySelector("
        "'.contacts-table tbody button')).transitionDuration"
    )
    patient_transition = patient.evaluate(
        "getComputedStyle(document.querySelector("
        "'form .button--secondary')).transitionDuration"
    )
    assert transition == "0s"
    assert patient_transition == "0s"
    _capture(patient, root, "patient-home-reduced-motion-375")
    return {
        "staff_button_transition": transition,
        "patient_button_transition": patient_transition,
    }


def test_reflow_forced_colors_reduced_motion_zoom_and_long_content(
    access_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    access_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    """The remaining matrix cells on the real staff and patient surfaces.

    320px reflow and long-content wrapping, forced colors, reduced motion
    and a 1280px window at 200% zoom; each scene watches both pages'
    consoles and records its measurements in the accessibility report.
    """
    root = renewal_artifact_root
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
        context = access_browser.new_context(locale="pt-BR", **options)
        page = context.new_page()
        page.set_default_timeout(20_000)
        errors = _watch_errors(page)
        try:
            _sign_in(page, renewal_base_url, access_staff)
            # The widths scene exercises the long-content patient; the
            # preference scenes use the ordinary enrollment.
            name = LONG_PATIENT if scene == "widths" else PATIENT_A
            _open_access(page, renewal_base_url, access_staff, name)
            code = _issue_code(page)

            patient, patient_errors, patient_context = _patient_scene(
                access_browser, options, renewal_base_url, access_staff, code, name
            )
            try:
                if scene == "widths":
                    report[scene] = _access_reflow(page, patient, root)
                elif scene == "forced_colors":
                    report[scene] = _access_forced_colors(page, patient, root)
                elif scene == "zoom_200":
                    report[scene] = _access_zoom_200(page, patient, root)
                else:
                    report[scene] = _reduced_motion(page, patient, root)
            finally:
                patient_context.close()
            assert not patient_errors, (scene, patient_errors)
            checks = browser_report["checks"]
            assert isinstance(checks, list)
            checks.append(
                {
                    "surface": "patient-access",
                    "scene": scene,
                    "assertion": "the populated staff access screen and the"
                    " bound patient home stay usable and overflow-free",
                    "console_errors": errors,
                    "patient_console_errors": patient_errors,
                }
            )
        finally:
            context.close()
        assert not errors, (scene, errors)

    report["not_applicable"] = {
        "dragging": (
            "no drag interaction exists on the staff access or patient"
            " surfaces; every action is a button or form"
        ),
        "loading_state": (
            "patient surfaces render complete documents; the only deferred"
            " region is the staff registry search covered by the staff-intake"
            " suite"
        ),
    }
    destination = root / "patient-access" / "accessibility-report.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)
