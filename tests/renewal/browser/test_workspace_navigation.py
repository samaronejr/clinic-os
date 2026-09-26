"""Real-browser IA shell (todo 13): role matrix, command palette, patient banner.

Part of the ``workspace`` suite. Owner access only seeds synthetic staff and
patients; the product runs as ``clinic_app``. Every wait subscribes to a
navigation, response or dialog state, never a timer.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._protected import encrypt
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_encounter import press
from renewal.browser.test_encounter import seed as seed_encounter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from playwright.sync_api import Browser, Error, Page, Response

__all__ = ("availability_staff",)

PATIENT_BASE: Final = "Helena Navegação Sintética"
FOREIGN_BASE: Final = "Otávio Vizinho Sintético"
CLINIC_B: Final = "Clínica Sintética Navegação B"
WIDTHS: Final = (1280, 375, 320)
MIN_TARGET_PX: Final = 44
PINNED_FROM_PX: Final = 768
OK: Final = 200
FORBIDDEN: Final = 403
NOT_FOUND: Final = 404
ENCOUNTER_DAY: Final = "2035-07-03"
AXE_URL: Final = "/static/vendor/axe/axe.min.js"
PRIVILEGED: Final = frozenset({"physician", "clinic_admin", "owner"})
# Plan annex IA x RP bundles v1 x the route guards the destinations lead to.
EXPECTED: Final = {
    "owner": ["agenda", "patients", "finance", "operations", "settings"],
    "clinic_admin": ["agenda", "patients", "finance", "operations", "settings"],
    "receptionist": ["agenda", "patients", "finance", "operations"],
    "physician": ["agenda", "patients", "operations"],
    "nurse": [],
    "allied_professional": [],
    "scheduler": [],
    "clinic_manager": [],
    "finance": [],
    "org_admin": [],
}
AXE_RUN_JS: Final = """async (include) => {
  const result = await axe.run(include ? {include} : document, {
    runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
      'wcag22aa', 'best-practice']},
    resultTypes: ['violations'],
  });
  return result.violations.map((v) => ({
    id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
  }));
}"""
# The shell chrome this todo owns, page-wide: skip link, header, section row,
# banner, palette and footer.
TARGETS_JS: Final = """(minimum) => {
  const selector = ['.skip-link', '.site-header a[href]', '.site-header summary',
    '.site-header button', '[data-patient-banner] button',
    '[data-patient-banner] a[href]', '.site-footer a[href]',
    'dialog[open] button', 'dialog[open] input:not([type=hidden])',
    'dialog[open] .combobox-option'].join(',');
  const small = [];
  let checked = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility({visibilityProperty: true})) continue;
    checked += 1;
    const rect = el.getBoundingClientRect();
    if (rect.width < minimum - 0.5 || rect.height < minimum - 0.5) {
      small.push([el.tagName, String(el.className).slice(0, 40),
        Math.round(rect.width), Math.round(rect.height)]);
    }
  }
  return {checked, small};
}"""
# Everything the browser keeps for the page: web storage and readable cookies.
STORAGE_JS: Final = """() => ({
  local: Object.fromEntries(Object.entries(localStorage)),
  session: Object.fromEntries(Object.entries(sessionStorage)),
  cookies: document.cookie,
})"""
# htmx (vendored) records the current path for its history support; that
# path carries the clinic only. No other key may appear.
ALLOWED_SESSION_KEYS: Final = frozenset({"htmx-current-path-for-history"})


@pytest.fixture
def nav_staff(renewal_base_url: str) -> dict[str, str]:
    """One synthetic user per role in clinic A, plus patients in A and in B."""
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    values = {
        "dsn": os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"],
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "clinic_b": str(uuid4()),
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "password": secrets.token_urlsafe(24),
        "patient": str(uuid4()),
        "enrollment": str(uuid4()),
        "foreign_patient": str(uuid4()),
        "foreign_enrollment": str(uuid4()),
    }
    suffix = uuid4().hex[:6]
    # Letters only (names reject digits); unique per test so exact matching
    # never sees an earlier test's patient.
    surname = "".join(chr(ord("a") + int(char, 16)) for char in suffix).title()
    values["patient_name"] = f"{PATIENT_BASE} {surname}"
    values["foreign_name"] = f"{FOREIGN_BASE} {surname}"
    with psycopg.connect(values["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [values["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_clinic "
            "(id, organization_id, name, crm_uf, timezone) "
            "VALUES (%s, %s, %s, 'SP', 'America/Sao_Paulo')",
            [values["clinic_b"], values["organization"], f"{CLINIC_B} {suffix}"],
        )
        for role in EXPECTED:
            user_id = str(uuid4())
            username = f"ia-{role.replace('_', '-')}-{suffix}"
            values[role] = username
            values[f"{role}_id"] = user_id
            connection.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [
                    user_id,
                    username,
                    make_password(values["password"]),
                    f"{username}@workspace.invalid",
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    str(uuid4()),
                    user_id,
                    values["organization"],
                    values["clinic_a"],
                    role,
                ],
            )
        for patient, enrollment, clinic, name in (
            (
                values["patient"],
                values["enrollment"],
                values["clinic_a"],
                values["patient_name"],
            ),
            (
                values["foreign_patient"],
                values["foreign_enrollment"],
                values["clinic_b"],
                values["foreign_name"],
            ),
        ):
            connection.execute(
                "INSERT INTO clinic_app.intake_patient "
                "(id,organization_id,full_name,birth_date,created_at) "
                "VALUES (%s,%s,%s,%s,now())",
                [
                    patient,
                    values["organization"],
                    encrypt(connection, "intake.patient.full_name", name.encode()),
                    encrypt(connection, "intake.patient.birth_date", b"1988-02-29"),
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.intake_patientclinicenrollment "
                "(id,organization_id,clinic_id,patient_id,idempotency_key,"
                "create_fingerprint,created_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
                [
                    enrollment,
                    values["organization"],
                    clinic,
                    patient,
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
        connection.execute("SET ROLE clinic_app")
        for role in PRIVILEGED:
            key = secrets.token_hex(20)
            values[f"{role}_totp"] = key
            connection.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                [values[f"{role}_id"]],
            )
            connection.execute(
                "INSERT INTO clinic_app.otp_totp_totpdevice "
                "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
                "user_id, throttling_failure_count, created_at) "
                "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
                "%s, 0, now())",
                [key, values[f"{role}_id"]],
            )
    return values


@pytest.fixture
def nav_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


def _folder(root: Path) -> Path:
    folder = root / "workspace-navigation"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def _capture(page: Page, root: Path, name: str, *, full_page: bool = True) -> str:
    destination = _folder(root) / f"{name}.png"
    page.screenshot(path=str(destination), full_page=full_page)
    destination.chmod(0o600)
    return destination.name


def _sign_in(page: Page, base: str, staff: dict[str, str], role: str) -> None:
    page.goto(f"{base}/auth/login/")
    page.locator("#id_username").fill(staff[role])
    page.locator("#id_password").fill(staff["password"])
    with page.expect_navigation():
        page.locator("button[type=submit]").click()
    if role in PRIVILEGED:
        page.wait_for_url("**/auth/verify/**")
        with psycopg.connect(staff["dsn"]) as connection:
            connection.execute("SET ROLE clinic_app")
            connection.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                [staff[f"{role}_id"]],
            )
            connection.execute(
                "UPDATE clinic_app.otp_totp_totpdevice SET last_t = -1 "
                "WHERE user_id = %s",
                [staff[f"{role}_id"]],
            )
        key = bytes.fromhex(staff[f"{role}_totp"])
        page.locator("#id_otp_token").fill(f"{TOTP(key, 30, 0, 6, 0).token():06d}")
        with page.expect_navigation():
            page.locator("button[type=submit]").click()
    page.wait_for_url("**/auth/protected/")


def _modules(page: Page) -> list[str]:
    modules = page.locator(".nav-list a[data-module]").evaluate_all(
        "links => links.map(link => link.dataset.module)"
    )
    return [str(module) for module in modules]


def _axe(page: Page, base: str, include: list[list[str]] | None = None) -> list[object]:
    if not page.evaluate("typeof window.axe !== 'undefined'"):
        with page.expect_response(f"{base}{AXE_URL}") as loaded:
            page.add_script_tag(url=f"{base}{AXE_URL}")
        assert loaded.value.status == OK
    violations = page.evaluate(AXE_RUN_JS, include)
    assert isinstance(violations, list)
    return violations


def _targets(page: Page) -> dict[str, object]:
    result = page.evaluate(TARGETS_JS, MIN_TARGET_PX)
    assert isinstance(result, dict)
    return result


def _no_overflow(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))


def _is_options(query: str) -> Callable[[Response], bool]:
    return lambda response: (
        response.url.endswith("/workspace/command/options/")
        and response.request.post_data is not None
        and f"q={query}" in response.request.post_data
    )


def errors_sink(errors: list[str]) -> Callable[[Error], None]:
    """Collect uncaught page errors of one context."""
    return lambda error: errors.append(str(error))


def _write(root: Path, name: str, payload: object) -> None:
    destination = _folder(root) / name
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    destination.chmod(0o600)


def test_every_role_sees_exactly_its_destinations(
    nav_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    nav_staff: dict[str, str],
) -> None:
    observed: dict[str, list[str]] = {}
    axe_results: dict[str, object] = {}
    for role, expected in EXPECTED.items():
        context = nav_browser.new_context(
            locale="pt-BR", viewport={"width": 1280, "height": 900}
        )
        page = context.new_page()
        try:
            _sign_in(page, renewal_base_url, nav_staff, role)
            observed[role] = _modules(page)
            assert observed[role] == expected, role
            expect(page.locator(".nav-command")).to_have_count(1 if expected else 0)
            for key in ("today", "inbox", "messages", "automations", "reports"):
                expect(page.locator(f'[data-module="{key}"]')).to_have_count(0)
            violations = _axe(page, renewal_base_url)
            axe_results[role] = violations
            assert violations == [], (role, violations)
            _capture(page, renewal_artifact_root, f"role-{role}-1280")
        finally:
            context.close()
    _write(
        renewal_artifact_root,
        "role-matrix.json",
        {"expected": EXPECTED, "observed": observed, "axe": axe_results},
    )


def test_physician_reaches_the_agenda_by_keyboard(
    nav_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    nav_staff: dict[str, str],
) -> None:
    video_dir = _folder(renewal_artifact_root) / "video"
    context = nav_browser.new_context(
        locale="pt-BR",
        viewport={"width": 1280, "height": 800},
        record_video_dir=str(video_dir),
        record_video_size={"width": 1280, "height": 800},
    )
    page = context.new_page()
    agenda = f"/scheduling/clinics/{nav_staff['clinic_a']}/agenda/"
    try:
        _sign_in(page, renewal_base_url, nav_staff, "physician")
        page.locator("h1").click()
        page.keyboard.press("Control+k")
        palette = page.locator("#command-palette")
        expect(palette).to_have_attribute("open", "")
        expect(page.locator("#command-palette-input")).to_be_focused()
        with page.expect_response(_is_options("agenda")) as found:
            page.keyboard.type("agenda")
        assert found.value.status == OK
        options = page.locator("#command-palette-input-listbox [role=option]")
        expect(options.first).to_contain_text(gettext("Agenda"))
        assert _targets(page)["small"] == []
        assert _axe(page, renewal_base_url) == []
        _capture(page, renewal_artifact_root, "palette-physician-1280", full_page=False)
        page.keyboard.press("ArrowDown")
        expect(page.locator("#command-palette-input")).to_have_attribute(
            "aria-activedescendant", re.compile(r"command-palette-input-opt-")
        )
        with page.expect_navigation():
            page.keyboard.press("Enter")
        assert page.url == f"{renewal_base_url}{agenda}"
        expect(page.locator('a[data-module="agenda"]')).to_have_attribute(
            "aria-current", "page"
        )
        # Escape closes and returns focus to the element that opened it.
        trigger = page.locator(".nav-command")
        trigger.focus()
        page.keyboard.press("Enter")
        expect(palette).to_have_attribute("open", "")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        expect(palette).not_to_have_attribute("open", "")
        expect(trigger).to_be_focused()
    finally:
        video = page.video
        context.close()
    assert video is not None
    video.save_as(str(_folder(renewal_artifact_root) / "palette.webm"))


def test_reception_pins_a_patient_from_the_palette_at_every_width(
    nav_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    nav_staff: dict[str, str],
) -> None:
    report: list[dict[str, object]] = []
    for width in WIDTHS:
        context = nav_browser.new_context(
            locale="pt-BR", viewport={"width": width, "height": 900}
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", errors_sink(errors))
        try:
            _sign_in(page, renewal_base_url, nav_staff, "receptionist")
            assert _no_overflow(page), width
            page.locator(".nav-command").click()
            expect(page.locator("#command-palette")).to_have_attribute("open", "")
            with page.expect_response(_is_options("Helena")) as found:
                page.locator("#command-palette-input").fill(nav_staff["patient_name"])
            assert found.value.status == OK
            option = page.locator("#command-palette-input-listbox [data-kind=patient]")
            expect(option).to_have_count(1)
            expect(option).to_contain_text(nav_staff["patient_name"])
            assert _targets(page)["small"] == [], width
            assert _axe(page, renewal_base_url) == [], width
            _capture(page, renewal_artifact_root, f"palette-patient-{width}")
            with page.expect_navigation():
                option.click()
            banner = page.locator("[data-patient-banner]")
            expect(banner).to_contain_text(nav_staff["patient_name"])
            expect(banner.locator(".patient-banner-meta")).to_contain_text(
                re.compile(r"^\d+ ")
            )
            assert nav_staff["patient_name"] not in page.title()
            assert nav_staff["enrollment"] not in page.url
            stored = page.evaluate(STORAGE_JS)
            assert stored["local"] == {}
            assert set(stored["session"]) <= ALLOWED_SESSION_KEYS
            kept = json.dumps(stored, ensure_ascii=False)
            for secret in (
                nav_staff["patient_name"],
                nav_staff["enrollment"],
                nav_staff["patient"],
            ):
                assert secret not in kept
            assert _no_overflow(page), width
            # The footer link is part of the checked chrome at every width.
            expect(page.locator(".site-footer a[href]")).to_have_count(1)
            targets = _targets(page)
            assert targets["small"] == [], (width, targets)
            assert _axe(page, renewal_base_url) == [], width
            _capture(page, renewal_artifact_root, f"banner-{width}")
            # From 48rem the banner stays pinned while the page scrolls.
            if width >= PINNED_FROM_PX:
                page.mouse.wheel(0, 2000)
                expect(banner).to_be_in_viewport()
            report.append({"width": width, "targets": targets, "errors": errors})
            assert not errors
        finally:
            context.close()
    _write(renewal_artifact_root, "banner-report.json", report)


def test_unsaved_note_holds_the_patient_until_discarded(
    nav_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    data = seed_encounter(staff, ENCOUNTER_DAY)
    context = nav_browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    base = renewal_base_url
    try:
        _sign_in_physician(page, base, staff)
        page.goto(
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{ENCOUNTER_DAY}/1/"
        )
        press(page, "open")
        page.locator("#template-id").select_option(data["specialty"])
        press(page, "template")
        banner = page.locator("[data-patient-banner]")
        expect(banner).to_contain_text("Paciente Sintético Questionário")
        expect(banner).to_contain_text(gettext("Encounter in progress"))
        assert "Questionário" not in page.title()
        page.locator("#id_subjective").fill("Relato sintético ainda não salvo")
        expect(page.locator("#save-state")).to_have_attribute("data-state", "unsaved")
        encounter_url = page.url

        page.locator(".patient-banner-close").click()
        dialog = page.locator("#context-switch-dialog")
        expect(dialog).to_have_attribute("open", "")
        assert _axe(page, base) == []
        _capture(page, renewal_artifact_root, "unsaved-dialog-1280", full_page=False)
        dialog.locator("[data-context-keep]").click()
        expect(dialog).not_to_have_attribute("open", "")
        assert page.url == encounter_url
        expect(page.locator("#id_subjective")).to_have_value(
            "Relato sintético ainda não salvo"
        )

        page.locator(".patient-banner-close").click()
        expect(dialog).to_have_attribute("open", "")
        with page.expect_navigation(url=re.compile(r"/agenda/")):
            dialog.locator("[data-context-discard]").click()
        expect(page.locator("[data-patient-banner]")).to_have_count(0)
        _capture(page, renewal_artifact_root, "after-discard-1280")
    finally:
        context.close()


def _api(page: Page, base: str, body: dict[str, str], *, csrf: bool = True) -> object:
    token = next(
        cookie["value"]
        for cookie in page.context.cookies()
        if cookie["name"] == "csrftoken"
    )
    response = page.request.post(
        f"{base}/api/ui/v1/command/search/",
        data=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            **({"X-CSRFToken": token} if csrf else {}),
        },
    )
    return response.status, response.json()


def test_finance_and_crafted_requests_get_nothing_extra(
    nav_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    nav_staff: dict[str, str],
) -> None:
    base = renewal_base_url
    outcomes: dict[str, object] = {}
    finance = nav_browser.new_context(locale="pt-BR")
    reception = nav_browser.new_context(locale="pt-BR")
    try:
        page = finance.new_page()
        _sign_in(page, base, nav_staff, "finance")
        outcomes["finance_patient"] = _api(
            page,
            base,
            {"q": nav_staff["patient_name"], "clinic_id": nav_staff["clinic_a"]},
        )
        assert outcomes["finance_patient"] == (OK, [])

        page = reception.new_page()
        _sign_in(page, base, nav_staff, "receptionist")
        nothing = _api(
            page,
            base,
            {"q": "Nome Sintético Inexistente", "clinic_id": nav_staff["clinic_a"]},
        )
        foreign_here = _api(
            page,
            base,
            {"q": nav_staff["foreign_name"], "clinic_id": nav_staff["clinic_a"]},
        )
        foreign_clinic = _api(
            page,
            base,
            {"q": nav_staff["foreign_name"], "clinic_id": nav_staff["clinic_b"]},
        )
        outcomes.update(
            nothing=nothing, foreign_here=foreign_here, foreign_clinic=foreign_clinic
        )
        assert nothing == foreign_here == foreign_clinic == (OK, [])
        no_csrf = _api(page, base, {"q": "agenda"}, csrf=False)
        outcomes["no_csrf"] = no_csrf
        assert no_csrf == (
            FORBIDDEN,
            {"code": "csrf_failed", "message_key": "api.error.csrf_failed"},
        )
        forged = page.request.post(
            f"{base}/workspace/command/run/",
            form={
                "csrfmiddlewaretoken": next(
                    c["value"] for c in reception.cookies() if c["name"] == "csrftoken"
                ),
                "token": secrets.token_urlsafe(18),
            },
        )
        outcomes["forged_token_status"] = forged.status
        assert forged.status == NOT_FOUND
        assert nav_staff["foreign_name"] not in forged.text()
    finally:
        finance.close()
        reception.close()
    _write(renewal_artifact_root, "adversarial.json", outcomes)


def test_palette_page_works_without_javascript(
    nav_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    nav_staff: dict[str, str],
) -> None:
    context = nav_browser.new_context(
        locale="pt-BR",
        viewport={"width": 375, "height": 900},
        java_script_enabled=False,
    )
    page = context.new_page()
    try:
        _sign_in(page, renewal_base_url, nav_staff, "receptionist")
        with page.expect_navigation():
            page.locator(".nav-command").click()
        page.locator("#command-q").fill(nav_staff["patient_name"])
        with page.expect_navigation():
            page.locator(".command-page-form button[type=submit]").click()
        choice = page.locator(
            ".command-page-list button", has_text=nav_staff["patient_name"]
        )
        expect(choice).to_have_count(1)
        _capture(page, renewal_artifact_root, "no-js-results-375")
        with page.expect_navigation():
            choice.click()
        expect(page.locator("[data-patient-banner]")).to_contain_text(
            nav_staff["patient_name"]
        )
        _capture(page, renewal_artifact_root, "no-js-banner-375")
    finally:
        context.close()
