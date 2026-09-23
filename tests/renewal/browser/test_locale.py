"""Real pt-BR registration and scheduling, served only as clinic_app.

Owner access below is confined to isolated synthetic staff setup and a durable
UTC/enum assertion. The runner removes the whole owned database after the suite.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.template.loader import render_to_string
from django.utils.translation import gettext
from playwright.sync_api import expect

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page, Route

NAME = "João da Conceição Sintético"
DAY = "2031-01-02"


@pytest.fixture
def locale_staff(renewal_base_url: str) -> dict[str, str]:
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    values = {
        "dsn": os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"],
        "clinic": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "username": f"locale-{uuid4().hex}",
        "password": secrets.token_urlsafe(24),
        "physician": str(uuid4()),
    }
    with psycopg.connect(values["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [values["organization"]],
        )
        for user_id, username, role in [
            (str(uuid4()), values["username"], "receptionist"),
            (values["physician"], f"medico-{uuid4().hex}", "physician"),
        ]:
            connection.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [
                    user_id,
                    username,
                    make_password(values["password"]),
                    f"{username}@locale.invalid",
                ],
            )
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                [str(uuid4()), user_id, values["organization"], values["clinic"], role],
            )
    return values


def _submit(page: Page, selector: str) -> None:
    """Subscribe before the action; no sleeps or polling delays."""
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as received:
        page.locator(selector).click()
    assert received.value.status in {200, 204, 302, 303}


def _capture(page: Page, root: Path, name: str) -> None:
    destination = root / "locale" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=True)
    destination.chmod(0o600)


def _sign_in(page: Page, base_url: str, staff: dict[str, str]) -> None:
    response = page.goto(f"{base_url}/readyz")
    assert response is not None
    assert response.json() == {"status": "ok"}
    page.goto(f"{base_url}/auth/login/")
    expect(page.locator("html")).to_have_attribute("lang", "pt-br")
    page.locator("#id_username").fill(staff["username"])
    page.locator("#id_password").fill(staff["password"])
    _submit(page, "button[type=submit]")
    page.wait_for_url("**/auth/protected/")


def _register(page: Page, patients: str, root: Path, mode: str) -> None:
    name = f"{NAME} {mode}"
    page.goto(patients + "new/")
    page.locator("#id_full_name").fill(name)
    # Exercise the server, not a native browser validation bubble.
    page.locator("#id_birth_date").evaluate(
        "el => { el.type = 'text'; el.form.noValidate = true; }"
    )
    page.locator("#id_birth_date").fill("31/02/2030")
    _submit(page, "button[type=submit]")
    expect(page.locator("#id_birth_date")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#id_birth_date_error")).to_have_text(
        gettext("Enter a valid date.")
    )
    expect(page.locator("main")).to_have_count(1)
    expect(page.locator(".site-header")).to_have_count(1)
    _capture(page, root, f"{mode}-invalid-date")
    page.locator("#id_birth_date").fill("1988-03-12")
    _submit(page, "button[type=submit]")
    page.wait_for_url(patients)
    page.locator("#id_q").fill(name)
    _submit(page, "#patient-search-form button[type=submit]")
    expect(page.locator(".intake-table tbody")).to_contain_text(name)
    expect(page.locator(".intake-table tbody")).to_contain_text("12/03/1988")
    assert name not in page.url
    assert "birth_date" not in page.url
    _capture(page, root, f"{mode}-patient")


@pytest.mark.parametrize("javascript", [True, False], ids=["enhanced", "native"])
def test_portuguese_patient_and_clinic_time_round_trip(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    locale_staff: dict[str, str],
    javascript: bool,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", timezone_id="Asia/Tokyo", java_script_enabled=javascript
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    mode = "enhanced" if javascript else "native"
    patients = f"{renewal_base_url}/intake/clinics/{locale_staff['clinic']}/patients/"
    scheduling = f"{renewal_base_url}/scheduling/clinics/{locale_staff['clinic']}/"
    try:
        _sign_in(page, renewal_base_url, locale_staff)
        _register(page, patients, renewal_artifact_root, mode)

        page.goto(scheduling + "availability/")
        page.locator("#id_practitioner").select_option(locale_staff["physician"])
        page.locator("#id_local_date").fill(DAY)
        page.locator("#id_start_time").fill("22:00")
        page.locator("#id_end_time").fill("23:59")
        _submit(page, "#scheduling-panel button[type=submit]")
        expect(page.locator(".availability-day-row time").first).to_have_attribute(
            "datetime", DAY
        )
        expect(page.locator(".availability-window th time").first).to_have_attribute(
            "datetime", DAY + "T22:00"
        )
        expect(page.locator(".availability-window th time").first).to_have_text("22:00")
        page.goto(patients)
        page.locator("#id_q").fill(f"{NAME} {mode}")
        _submit(page, "#patient-search-form button[type=submit]")
        expect(page.locator(".intake-table tbody tr").first).to_be_visible()
        _submit(page, ".intake-table tbody tr:first-child button.button--secondary")
        expect(page.locator("#scheduling-booking")).to_be_visible()
        page.locator("#id_practitioner").select_option(locale_staff["physician"])
        page.locator("#id_start_local").fill(DAY + "T23:00")
        page.locator("#id_end_local").fill(DAY + "T23:30")
        _submit(page, "#scheduling-booking button[type=submit]")
        page.wait_for_url("**/agenda/day/2031-01-02/1/")
        expect(page.locator(".agenda-table .agenda-time time").first).to_have_attribute(
            "datetime", DAY + "T23:00"
        )
        expect(page.locator(".agenda-table .agenda-time time").first).to_have_text(
            "23:00"
        )
        expect(page.locator(".agenda-table")).to_contain_text(gettext("Scheduled"))
        for width in (375, 768, 1280, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            _capture(page, renewal_artifact_root, f"{mode}-agenda-{width}")
        with psycopg.connect(locale_staff["dsn"]) as connection:
            connection.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [locale_staff["organization"]],
            )
            row = connection.execute(
                "SELECT start_at, end_at, status "
                "FROM clinic_app.scheduling_appointment WHERE practitioner_id = %s",
                [locale_staff["physician"]],
            ).fetchone()
        assert row == (
            datetime(2031, 1, 3, 2, tzinfo=UTC),
            datetime(2031, 1, 3, 2, 30, tzinfo=UTC),
            "scheduled",
        )
        assert not errors
        report = {
            "mode": mode,
            "runtime_role": "clinic_app (readyz verified)",
            "browser_zone": "Asia/Tokyo",
            "clinic_zone": "America/Sao_Paulo",
            "utc_round_trip": True,
            "console_errors": errors,
            "reflow_widths": [375, 768, 1280, 320],
            "field_error_association": True,
        }
        (renewal_artifact_root / "locale" / f"{mode}-report.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
    finally:
        context.close()


def test_synthetic_brl_display_and_denied_surface(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    response = renewal_page.goto(
        f"{renewal_base_url}/intake/clinics/{uuid4()}/patients/"
    )
    assert response is not None
    assert response.status == 403
    expect(renewal_page.locator("h1")).to_have_text(gettext("Access unavailable"))
    _capture(renewal_page, renewal_artifact_root, "denied")
    # No billing route exists: render the shipped, inert showcase exactly as
    # the primitives suite does. Styles still come from the real server.
    url = f"{renewal_base_url}/__locale_example__/"

    def document(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_to_string("identity/showcase.html"),
        )

    renewal_page.route(url, document)
    try:
        renewal_page.goto(url)
        expect(renewal_page.locator("#brl-example")).to_have_text("R$ 1.234,56")
        renewal_page.locator("[data-stress=brl-example]").screenshot(
            path=str(renewal_artifact_root / "locale" / "brl-example.png")
        )
    finally:
        renewal_page.unroute(url, document)
