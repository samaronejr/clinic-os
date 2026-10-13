"""Private fixtures and native interactions for the onboarding browser suite."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import (
    FixtureSecret,
    fixture_dsn,
    new_password,
)
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._page_wait import await_autofocus, evaluate_js
from renewal.browser._protected import encrypt
from renewal.browser.engines import full_page_screenshot

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import BrowserContext, Page, Route

CLINIC_A: Final = "Clínica Vila Mariana"
CLINIC_ZONE: Final = "America/Sao_Paulo"
WIDTHS: Final = (320, 768, 1280)
PRIVATE_CSS: Final = """
input:not([type=hidden]), textarea, select { color: transparent !important; }
.nav-user, .nav-clinic, #booking-patient, .agenda-patient, .agenda-physician,
.intake-table th[scope=row], .intake-table td[data-label],
.booking-windows td:first-child,
.panel > ul > li, .panel > ul > li h3 { color: transparent !important; }
img[src^="data:"] { visibility: hidden !important; }
[data-private-recovery-codes] code { color: transparent !important; background: black; }
[data-session-record] p { color: transparent !important; }
"""
INVITATION_ERROR: Final = (
    "We could not accept this invitation. Check the private details."
)


@pytest.fixture(scope="session")
def onboarding_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed one TOTP-enrolled physician; every seeded name is run-unique.

    Module-bound fixture definitions instantiate once per importing module,
    so no seeded row may carry a constant value across the suite's files.
    """
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    suffix = uuid4().hex[:4]
    values = {
        "dsn": fixture_dsn(),
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "physician_a": f"dra-ana-sintetica-{suffix}",
        "physician_a_id": str(uuid4()),
        "password": new_password(),
    }
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
            "INSERT INTO clinic_app.identity_user "
            "(id, username, password, email, first_name, last_name, is_active, "
            "is_staff, is_superuser, date_joined) "
            "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
            [
                values["physician_a_id"],
                values["physician_a"],
                make_password(values["password"]),
                f"{values['physician_a']}@onboarding.invalid",
            ],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id, user_id, organization_id, clinic_id, role, "
            "workflow_bundle_version) "
            "VALUES (%s, %s, %s, %s, 'physician', 1)",
            [
                str(uuid4()),
                values["physician_a_id"],
                values["organization"],
                values["clinic_a"],
            ],
        )
    return values


def redact_video(context: BrowserContext) -> None:
    """Mask private paint before the first frame without changing DOM or layout."""

    def stylesheet(route: Route) -> None:
        response = route.fetch()
        route.fulfill(response=response, body=response.text() + PRIVATE_CSS)

    context.route("**/css/clinic-os-auth.css", stylesheet)


def capture(page: Page, root: Path, scene: str) -> None:
    """Capture masked geometry using the repository's engine-safe splitter."""
    root.mkdir(exist_ok=True, mode=0o700)
    full_page_screenshot(page, root / f"{scene}.png")
    assert evaluate_js(page, "document.documentElement.scrollWidth <= innerWidth")


def targets(page: Page, selector: str) -> None:
    boxes = page.locator(selector).evaluate_all(
        "els => els.map(e => { const b = e.getBoundingClientRect(); "
        "return [b.width, b.height]; })"
    )
    assert boxes
    assert all(width >= 44 and height >= 44 for width, height in boxes), boxes


def submit(page: Page, selector: str) -> int:
    response = click_to_navigate(page.locator(selector), hittable=True)
    assert response is not None
    return response.status


def sign_out(page: Page, base: str) -> None:
    """Finish the logout confirmation; a GET alone never ends the session."""
    goto_settled(page, base + "/auth/logout/")
    assert submit(page, "form button[type=submit]") == 200


def invite(page: Page, email: str) -> str:
    page.locator("#id_email").fill(email)
    page.locator("#id_bundle").select_option("receptionist")
    assert submit(page, '#invite-title + form button[type="submit"]') == 200
    expect(page.locator("[data-invitation-queued]")).to_be_visible()
    return page.locator('input[name="invitation_id"]').first.input_value()


def invitation_details(staff: dict[str, str], invitation_id: str) -> tuple[str, str]:
    """Rehydrate the actual private email code, never add a URL token."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT id,organization_id,pending_user_id,token_nonce,token_hash "
            "FROM clinic_app.identity_staffinvitation WHERE id=%s",
            [invitation_id],
        ).fetchone()
    assert row is not None
    value = ":".join(str(part) for part in row[:4])
    token = FixtureSecret(
        salted_hmac("identity.staff_invitation", value, algorithm="sha256").hexdigest()
    )
    assert hmac.compare_digest(
        hashlib.sha256(token.encode()).digest(), bytes(row[4])
    ), "Invitation delivery signing key is not shared with the fixture."
    return token, str(row[2])


def enrollment_fields(page: Page, token: str, username: str, password: str) -> None:
    page.locator("#id_invitation_code").fill(token)
    page.locator("#id_username").fill(username)
    page.locator("#id_password").fill(password)
    page.locator("#id_password_confirmation").fill(password)


def confirm_enrollment(page: Page, staff: dict[str, str], user_id: str) -> None:
    """Read the owned fixture's private seed and submit a real current TOTP."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute("SET ROLE clinic_app")
        conn.execute("SELECT set_config('app.current_user_id', %s, true)", [user_id])
        row = conn.execute(
            "SELECT key FROM clinic_app.otp_totp_totpdevice WHERE user_id=%s",
            [user_id],
        ).fetchone()
    assert row is not None
    key = FixtureSecret(str(row[0]))
    token = TOTP(bytes.fromhex(key), 30, 0, 6, 1).token()
    page.locator("#id_otp_token").fill(FixtureSecret(f"{token:06d}"))
    assert submit(page, 'form button[type="submit"]') == 200


def book_first(page: Page, base: str, staff: dict[str, str], width: int) -> None:
    """A real receptionist creates a patient, selects it, and books a slot."""
    day = {320: "2036-06-17", 768: "2036-06-18", 1280: "2036-06-19"}[width]
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock "
            "(id,organization_id,clinic_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,created_at,updated_at,retired_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now(),NULL)",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                staff["physician_a_id"],
                f"{day}T12:00:00Z",
                f"{day}T13:00:00Z",
                str(uuid4()),
                hashlib.sha256(day.encode()).digest(),
            ],
        )
    patients = f"{base}/intake/clinics/{staff['clinic_a']}/patients/"
    name = f"Paciente Sintetico Onboarding {width}"
    goto_settled(page, patients + "new/")
    page.locator("#id_full_name").fill(name)
    page.locator("#id_birth_date").fill("1990-05-17")
    assert submit(page, "#patient-create-panel button[type=submit]") == 200
    expect(page.locator("#intake-registered")).to_be_visible()
    page.locator("#id_q").fill(name)
    assert submit(page, "#patient-search-form button[type=submit]") == 200
    expect(page.locator(".intake-table tbody tr").filter(has_text=name)).to_have_count(
        1
    )
    assert (
        submit(page, '.intake-table tbody tr form input[name="mode"] + input + button')
        == 200
    )
    expect(page.locator("#booking-patient")).to_have_text(name)
    page.locator("#id_practitioner").select_option(staff["physician_a_id"])
    page.locator("#id_start_local").fill(f"{day}T09:00")
    page.locator("#id_end_local").fill(f"{day}T09:30")
    assert submit(page, "#scheduling-booking button[type=submit]") == 200
    expect(page.locator("#appointment-booked")).to_contain_text(
        gettext("Appointment booked")
    )
    expect(page.locator(".agenda-table tbody tr").filter(has_text=name)).to_have_count(
        1
    )


def historical_invitation(staff: dict[str, str], manager_id: str, email: str) -> str:
    """Insert genuine expired history; never update immutable expiry or clocks."""
    token = FixtureSecret(uuid4().hex + uuid4().hex)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        user_id = str(uuid4())
        conn.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id,username,password,email,first_name,last_name,is_active,is_staff,"
            "is_superuser,date_joined) "
            "VALUES (%s,%s,'!','','','',false,false,false,now())",
            [user_id, f"pending-{uuid4().hex}"],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_staffinvitation "
            "(id,organization_id,clinic_id,pending_user_id,invited_by_id,email_hash,"
            "email,bundle,token_hash,token_nonce,state,expires_at,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'receptionist',%s,%s,'pending',"
            "now()-interval '1 day',now()-interval '3 days')",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                user_id,
                manager_id,
                salted_hmac(
                    "identity.staff_invitation.email", email, algorithm="sha256"
                ).digest(),
                encrypt(conn, "identity.staff.invitation.email", email.encode()),
                hashlib.sha256(token.encode()).digest(),
                str(uuid4()),
            ],
        )
    return token


def invitation_refused(
    page: Page, base: str, token: str, credentials: tuple[str, str]
) -> None:
    goto_settled(page, base + "/auth/invitation/")
    enrollment_fields(page, token, *credentials)
    assert submit(page, "form button[type=submit]") == 400
    await_autofocus(page.locator("[data-focus-error]"))
    expect(page.locator("[data-focus-error]")).to_contain_text(
        gettext(INVITATION_ERROR)
    )
    expect(page.locator("[data-focus-error]")).to_contain_text(
        gettext(
            "For an expired or unavailable invitation, ask your clinic "
            "administrator for a new invitation."
        )
    )
    expect(page.locator('img[src^="data:"]')).to_have_count(0)
