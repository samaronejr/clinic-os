"""Real-browser contacts and preferences journey, served as ``clinic_app``.

Owner access is confined to synthetic staff and registry setup. Every wait
subscribes to a navigation, a response or a DOM state, never a timer.
Captures contain synthetic names and masked destinations only.

Evidence grid: the journey test runs once per matrix width (1280, 768 and
375) and names every capture ``<state>-<width>``; each width owns one
channel so repeated runs never share contact state. The failure test adds
the denied, invalid and cross-patient states at the same widths.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._protected import encrypt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Locator, Page

CLINIC_A = "Clínica Vila Mariana"
CLINIC_B = "Clínica Vila Olímpia"
PATIENT_A = "Marina Sintética Contatos"
PATIENT_B = "Bia Sintética Contatos"
PATIENT_C = "Célia Sintética Contatos"
FOREIGN_NAME = "Marina Sintética Estrangeira"
PHONE = "+55 11 98765-4321"
PHONE_CHANGED = "+55 11 96666-7777"
EMAIL_A = "marina.contatos@example.com"
EMAIL_A_CHANGED = "marina.nova@clinica.example.org"
MASKED_PHONE = "••• •••• 4321"
MASKED_PHONE_CHANGED = "••• •••• 7777"
MASKED_EMAIL = "m•••@e•••.com"
MASKED_EMAIL_CHANGED = "m•••@c•••.org"
MATRIX_WIDTHS = (1280, 768, 375)
WIDTHS = (1280, 768, 640, 375, 320)
ZOOM_WINDOW = 1280  # physical window width behind the 200% zoom scene
ZOOM_FACTOR = 2
MIN_TARGET_PX = 44
# Each width owns one channel: (label msgid, value, destination, masked,
# changed, changed masked). Repeated matrix runs never share contact state.
WIDTH_CHANNELS: Final = {
    1280: ("SMS", "sms", PHONE, MASKED_PHONE, PHONE_CHANGED, MASKED_PHONE_CHANGED),
    768: (
        "Email",
        "email",
        EMAIL_A,
        MASKED_EMAIL,
        EMAIL_A_CHANGED,
        MASKED_EMAIL_CHANGED,
    ),
    375: (
        "WhatsApp",
        "whatsapp",
        PHONE,
        MASKED_PHONE,
        PHONE_CHANGED,
        MASKED_PHONE_CHANGED,
    ),
}


def _channel_label(msgid: str) -> str:
    """Return the rendered pt-BR channel label for one msgid."""
    return gettext(msgid)


OK = 200
NOT_FOUND = 404
CONTACTS_TITLE = "Contacts and messaging preferences"
SETTLED_JS = (
    "!document.querySelector('.htmx-request, .htmx-settling, .htmx-added,"
    ' [aria-busy="true"]\')'
)


@pytest.fixture(scope="session")
def contacts_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed a clinic-A receptionist, two patients in A and one in clinic B."""
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
        (values["clinic_a"], PATIENT_A, "1990-05-17"),
        (values["clinic_a"], PATIENT_B, "1985-03-09"),
        (values["clinic_a"], PATIENT_C, "1992-11-23"),
        (values["clinic_b"], FOREIGN_NAME, "1988-03-12"),
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
                f"{values['receptionist']}@contacts.invalid",
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
        # The accessibility scenes own patient C: one saved, unverified SMS
        # destination plus its history row, seeded once so every scene
        # renders the populated manage screen deterministically.
        contact_id = str(uuid4())
        connection.execute(
            "INSERT INTO clinic_app.intake_patientcontact "
            "(id, organization_id, patient_id, channel, destination, "
            "destination_version, verified_version, verified_at, "
            "verification_method, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'sms', %s, 1, 0, NULL, '', now(), now())",
            [
                contact_id,
                values["organization"],
                patients[PATIENT_C],
                encrypt(
                    connection,
                    "intake.patientcontact.destination",
                    b"+55 11 90000-1111",
                ),
            ],
        )
        connection.execute(
            "INSERT INTO clinic_app.intake_patientcontactevent "
            "(id, organization_id, clinic_id, patient_id, actor_id, "
            "actor_label, event_type, contact_id, preference_id, version, "
            "created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'contact_saved', %s, NULL, 1, "
            "now())",
            [
                str(uuid4()),
                values["organization"],
                values["clinic_a"],
                patients[PATIENT_C],
                values["receptionist_id"],
                values["receptionist"],
                contact_id,
            ],
        )
    values["enrollment_a"] = enrollments[PATIENT_A]
    values["enrollment_b"] = enrollments[PATIENT_B]
    values["enrollment_c"] = enrollments[PATIENT_C]
    return values


@pytest.fixture
def contacts_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


@pytest.fixture(params=MATRIX_WIDTHS, ids=[f"{width}px" for width in MATRIX_WIDTHS])
def journey(
    request: pytest.FixtureRequest, contacts_browser: Browser
) -> Iterator[Page]:
    """One pt-BR context per matrix width; tests read the width back."""
    context = contacts_browser.new_context(
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
    destination = root / "contacts" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=full_page)
    destination.chmod(0o600)
    return destination.name


def _no_overflow(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))


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


def _open_contacts(page: Page, base_url: str, staff: dict[str, str], name: str) -> None:
    """Search the patient and open the contacts screen through the row action."""
    patients = f"/intake/clinics/{staff['clinic_a']}/patients/"
    page.goto(f"{base_url}{patients}")
    page.locator("#id_q").fill(name)
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as received:
        page.locator("#patient-search-form button[type=submit]").click()
    assert received.value.status == OK
    page.wait_for_function(SETTLED_JS)
    row = page.locator(".intake-table tbody tr", has_text=name)
    with page.expect_navigation():
        row.get_by_role("button", name=re.compile(r"^Contatos")).click()
    expect(page.locator("h1")).to_have_text(gettext(CONTACTS_TITLE))


def _save_destination(
    page: Page, channel_label: str, destination: str, *, expect_error: bool = False
) -> None:
    """Open the edit screen for one channel and submit the destination."""
    item = page.locator(
        ".contacts-item", has=page.locator("h3", has_text=channel_label)
    )
    with page.expect_navigation():
        item.locator("form").first.locator("button").click()
    expect(page.locator("h1")).to_contain_text(gettext("Edit"))
    page.locator("#id_destination").fill(destination)
    with page.expect_navigation():
        page.locator("button", has_text=gettext("Save destination")).click()
    if not expect_error:
        expect(page.locator("h1")).to_have_text(gettext(CONTACTS_TITLE))


def _contact_item(page: Page, channel_label: str) -> Locator:
    return page.locator(
        ".contacts-item", has=page.locator("h3", has_text=channel_label)
    )


def _verify(page: Page, channel_label: str) -> None:
    item = _contact_item(page, channel_label)
    with page.expect_navigation():
        item.locator("button", has_text=gettext("Mark as verified")).click()


def _choose_and_revoke(
    page: Page,
    value: str,
    width: int,
    contacts_staff: dict[str, str],
    root: Path,
) -> None:
    """Choose the channel for reminders, revoke it and check the receipt."""
    purpose = page.locator(
        ".contacts-purpose",
        has=page.locator("legend", has_text=gettext("Appointment reminders")),
    )
    purpose.locator(f"input[name=channel][value={value}]").check()
    with page.expect_navigation():
        purpose.locator("button", has_text=gettext("Save preference")).click()
    expect(page.locator(".feedback--success")).to_contain_text(
        gettext("Messaging preferences saved.")
    )
    expect(purpose.locator(f"input[name=channel][value={value}]")).to_be_checked()
    _capture(page, root, f"manage-preference-chosen-{width}")

    purpose.locator("input[name=channel][value='']").check()
    with page.expect_navigation():
        purpose.locator("button", has_text=gettext("Save preference")).click()
    expect(page.locator(".feedback--success")).to_contain_text(
        gettext("Automated messages for this purpose were revoked.")
    )
    expect(purpose.locator("input[name=channel][value='']")).to_be_checked()

    # The revocation receipt is visible in the append-only history.
    history = page.locator(".contacts-table")
    expect(history).to_contain_text(gettext("Messages revoked"))
    expect(history).to_contain_text(contacts_staff["receptionist"])
    assert _no_overflow(page)
    _capture(page, root, f"manage-revoked-history-{width}")


def test_receptionist_verifies_chooses_and_revokes_a_channel(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    contacts_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    msgid, value, destination, masked, changed, changed_masked = WIDTH_CHANNELS[width]
    label = _channel_label(msgid)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    _sign_in(page, renewal_base_url, contacts_staff)
    _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_A)

    # Blank state: every channel shows no destination and nothing leaks.
    expect(page.locator(".contacts-item")).to_have_count(3)
    expect(page.locator(".contacts-status--empty").first).to_have_text(
        gettext("No destination recorded")
    )
    assert _no_overflow(page)
    _capture(page, root, f"manage-blank-{width}")

    # Edit shows the full destination; the manage screen masks it.
    _save_destination(page, label, destination)
    item = _contact_item(page, label)
    expect(item.locator(".contacts-destination code")).to_have_text(masked)
    expect(item.locator(".contacts-status--pending")).to_have_text(
        gettext("Not verified")
    )
    body = page.content()
    assert destination not in body
    assert _no_overflow(page)
    _capture(page, root, f"manage-saved-masked-{width}")

    # Verify: the receipt names the outcome and the status flips.
    _verify(page, label)
    expect(page.locator(".feedback--success")).to_contain_text(
        gettext("Destination verified for automated messages.")
    )
    expect(item.locator(".contacts-status--verified")).to_have_text(gettext("Verified"))
    _capture(page, root, f"manage-verified-{width}")

    _choose_and_revoke(page, value, width, contacts_staff, root)

    # Changing the destination invalidates verification visibly.
    _save_destination(page, label, changed)
    item = _contact_item(page, label)
    expect(item.locator(".contacts-destination code")).to_have_text(changed_masked)
    expect(item.locator(".contacts-status--pending")).to_have_text(
        gettext("Not verified")
    )
    expect(page.locator(".contacts-table")).to_contain_text(
        gettext("Verification invalidated by a destination change")
    )
    assert _no_overflow(page)
    _capture(page, root, f"manage-invalidated-{width}")

    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "contacts",
            "width": width,
            "channel": value,
            "assertion": "blank manage state, masked destination outside the"
            " edit screen, verify receipt, channel choice, revocation receipt"
            " in history, destination change invalidating verification",
            "console_errors": errors,
        }
    )


def _foreign_clinic_denied(
    page: Page,
    renewal_base_url: str,
    contacts_staff: dict[str, str],
    width: int,
    root: Path,
) -> None:
    """Another clinic's contacts endpoint is denied on GET and POST."""
    foreign = f"/intake/clinics/{contacts_staff['clinic_b']}/contacts/"
    response = page.goto(f"{renewal_base_url}{foreign}")
    assert response is not None
    assert response.status == NOT_FOUND
    body = page.content()
    assert CLINIC_B not in body
    assert FOREIGN_NAME not in body
    _capture(page, root, f"foreign-clinic-denied-{width}")
    page.goto(
        f"{renewal_base_url}/intake/clinics/{contacts_staff['clinic_a']}/patients/"
    )
    token = page.locator("input[name=csrfmiddlewaretoken]").first.get_attribute("value")
    assert token
    refused = page.request.post(
        f"{renewal_base_url}{foreign}",
        form={
            "csrfmiddlewaretoken": token,
            "action": "edit",
            "enrollment_id": contacts_staff["enrollment_a"],
            "channel": "sms",
        },
    )
    assert refused.status == NOT_FOUND
    assert FOREIGN_NAME not in refused.text()


def test_failures_mask_deny_and_never_cross_patients(
    journey: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    contacts_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = journey
    width = _width(page)
    msgid, value, destination, masked, _, _ = WIDTH_CHANNELS[width]
    label = _channel_label(msgid)
    root = renewal_artifact_root
    errors = _watch_errors(page)
    _sign_in(page, renewal_base_url, contacts_staff)

    # Patient B gets the identical destination; verifying A never verifies B.
    _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_A)
    _save_destination(page, label, destination)
    _verify(page, label)
    expect(
        _contact_item(page, label).locator(".contacts-status--verified")
    ).to_have_text(gettext("Verified"))
    _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_B)
    _save_destination(page, label, destination)
    item = _contact_item(page, label)
    expect(item.locator(".contacts-destination code")).to_have_text(masked)
    expect(item.locator(".contacts-status--pending")).to_have_text(
        gettext("Not verified")
    )
    _capture(page, root, f"cross-patient-unverified-{width}")

    # Invalid destination: the alert names the fix and keeps the input.
    _save_destination(page, _channel_label("Email"), "not-an-email", expect_error=True)
    alert = page.locator("#contact-errors")
    expect(alert).to_have_attribute("role", "alert")
    expect(alert).to_contain_text(
        gettext("Enter a valid destination for this channel.")
    )
    expect(page.locator("#id_destination")).to_have_value("not-an-email")
    assert _no_overflow(page)
    _capture(page, root, f"edit-invalid-{width}")
    with page.expect_navigation():
        page.locator("button", has_text=gettext("Back to contacts")).click()
    expect(page.locator("h1")).to_have_text(gettext(CONTACTS_TITLE))

    _foreign_clinic_denied(page, renewal_base_url, contacts_staff, width, root)

    # The blank GET state carries no patient data.
    response = page.goto(
        f"{renewal_base_url}/intake/clinics/{contacts_staff['clinic_a']}/contacts/"
    )
    assert response is not None
    assert response.status == OK
    assert PATIENT_A not in page.content()
    assert PATIENT_B not in page.content()
    assert _no_overflow(page)
    _capture(page, root, f"contacts-blank-get-{width}")

    # The only console entry is the refused clinic-B document itself.
    assert len(errors) == 1, errors
    assert re.search(r"\b404\b", errors[0])
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "contacts",
            "width": width,
            "channel": value,
            "assertion": "identical destination on another patient stays"
            " unverified, invalid destination keeps input, foreign clinic"
            " denied on GET and POST, blank GET carries no patient data",
            "console_errors": errors,
        }
    )


def test_native_post_completes_the_journey_without_javascript(
    contacts_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    contacts_staff: dict[str, str],
) -> None:
    context = contacts_browser.new_context(
        locale="pt-BR",
        viewport={"width": 768, "height": 1024},
        java_script_enabled=False,
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    try:
        _sign_in(page, renewal_base_url, contacts_staff)
        _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_B)
        email_label = _channel_label("Email")
        _save_destination(page, email_label, "bia.contatos@example.com")
        item = _contact_item(page, email_label)
        expect(item.locator(".contacts-destination code")).to_have_text("b•••@e•••.com")
        _verify(page, email_label)
        expect(
            _contact_item(page, email_label).locator(".contacts-status--verified")
        ).to_have_text(gettext("Verified"))
        purpose = page.locator(
            ".contacts-purpose",
            has=page.locator("legend", has_text=gettext("Booking confirmations")),
        )
        purpose.locator("input[name=channel][value=email]").check()
        with page.expect_navigation():
            purpose.locator("button", has_text=gettext("Save preference")).click()
        expect(page.locator(".feedback--success")).to_contain_text(
            gettext("Messaging preferences saved.")
        )
        assert _no_overflow(page)
        _capture(page, renewal_artifact_root, "native-journey-768")
    finally:
        context.close()


# --------------------------------------------------------------------------
# Keyboard, stale conflicts and the accessibility matrix
# --------------------------------------------------------------------------


def test_keyboard_reaches_and_activates_the_verify_action(
    contacts_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    contacts_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    """Tab order reaches every contact action and Enter submits it."""
    context = contacts_browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    errors = _watch_errors(page)
    try:
        _sign_in(page, renewal_base_url, contacts_staff)
        _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_B)
        _save_destination(page, _channel_label("SMS"), "+55 11 95555-4444")
        item = _contact_item(page, _channel_label("SMS"))
        expect(item.locator(".contacts-status--pending")).to_have_text(
            gettext("Not verified")
        )
        verify_button = item.locator("button", has_text=gettext("Mark as verified"))

        # Every tab stop lands on a visible control with a visible ring.
        focused: list[str] = []
        for _ in range(40):
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
            if verify_button.evaluate(
                "(element) => element === document.activeElement"
            ):
                break
        else:
            pytest.fail("verify action never received keyboard focus")

        with page.expect_navigation():
            page.keyboard.press("Enter")
        expect(page.locator(".feedback--success")).to_contain_text(
            gettext("Destination verified for automated messages.")
        )
        expect(item.locator(".contacts-status--verified")).to_have_text(
            gettext("Verified")
        )
        _capture(page, renewal_artifact_root, "keyboard-verified-1280")
    finally:
        context.close()
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "contacts",
            "width": 1280,
            "assertion": "keyboard traversal reaches the verify action with a"
            " visible focus ring at every stop and Enter submits it",
            "console_errors": errors,
        }
    )


def _save_from_second_tab(
    context: BrowserContext,
    renewal_base_url: str,
    contacts_staff: dict[str, str],
    destination: str,
) -> None:
    """Change patient C's SMS destination through a second open tab."""
    other = context.new_page()
    try:
        _open_contacts(other, renewal_base_url, contacts_staff, PATIENT_C)
        _save_destination(other, _channel_label("SMS"), destination)
    finally:
        other.close()


def test_stale_save_and_verify_actions_render_the_conflict(
    contacts_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    contacts_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    """A form rendered before a concurrent change conflicts instead of
    overwriting or verifying the unseen destination."""
    context = contacts_browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    errors = _watch_errors(page)
    try:
        _sign_in(page, renewal_base_url, contacts_staff)
        _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_C)
        sms = _contact_item(page, _channel_label("SMS"))

        # Stale save: page 1 holds the edit form for version 1 while a
        # second tab saves version 2 first.
        with page.expect_navigation():
            sms.locator("button", has_text=gettext("Edit destination")).click()
        expect(page.locator("h1")).to_contain_text(gettext("Edit"))
        _save_from_second_tab(
            context, renewal_base_url, contacts_staff, "+55 11 90000-2222"
        )
        page.locator("#id_destination").fill("+55 11 90000-3333")
        with page.expect_navigation():
            page.locator("button", has_text=gettext("Save destination")).click()
        alert = page.locator("#contact-errors")
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text(
            gettext(
                "This contact changed since you opened it. Review the "
                "current destination and save again."
            )
        )
        assert _no_overflow(page)
        _capture(page, renewal_artifact_root, "edit-stale-conflict-1280")

        # Stale verify: the manage screen rendered version 2; the
        # destination moved to version 3 before the verify POST.
        with page.expect_navigation():
            page.locator("button", has_text=gettext("Back to contacts")).click()
        expect(page.locator("h1")).to_have_text(gettext(CONTACTS_TITLE))
        _save_from_second_tab(
            context, renewal_base_url, contacts_staff, "+55 11 90000-4444"
        )
        with page.expect_navigation():
            sms.locator("button", has_text=gettext("Mark as verified")).click()
        alert = page.locator(".feedback--error")
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text(
            gettext(
                "This destination changed since you opened it. Review the "
                "current destination and verify it again."
            )
        )
        expect(sms.locator(".contacts-status--pending")).to_have_text(
            gettext("Not verified")
        )
        assert _no_overflow(page)
        _capture(page, renewal_artifact_root, "verify-stale-conflict-1280")
    finally:
        context.close()
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "contacts",
            "width": 1280,
            "assertion": "stale save and stale verify forms render the"
            " conflict alert and leave the replacement destination"
            " unverified",
            "console_errors": errors,
        }
    )


def _reflow(page: Page, root: Path) -> dict[str, object]:
    """The populated manage screen at every width down to the 320px floor."""
    displays: dict[int, str] = {}
    heights: dict[int, float] = {}
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        displays[width] = str(
            page.evaluate(
                "getComputedStyle(document.querySelector("
                "'.contacts-table tbody tr')).display"
            )
        )
        heights[width] = float(
            page.evaluate(
                "document.querySelector('.contacts-item-actions .button')"
                ".getBoundingClientRect().height"
            )
        )
        assert _no_overflow(page), width
        assert heights[width] >= MIN_TARGET_PX, (width, heights[width])
        _capture(page, root, f"manage-reflow-{width}", full_page=False)
    assert displays[1280] == displays[768] == "table-row"
    assert displays[640] == displays[375] == displays[320] == "block"
    return {
        str(width): {"row_display": displays[width], "button_height": heights[width]}
        for width in WIDTHS
    }


def _forced_colors(page: Page, root: Path) -> dict[str, object]:
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    item_border = page.evaluate(
        "getComputedStyle(document.querySelectorAll('.contacts-item')[1])"
        ".borderTopStyle"
    )
    assert item_border == "solid"
    row_border = page.evaluate(
        "getComputedStyle(document.querySelector('.contacts-table tbody tr th'))"
        ".borderBottomStyle"
    )
    assert row_border == "solid"
    page.keyboard.press("Tab")
    ring = _ring(page)
    assert ring["style"] == "solid"
    _capture(page, root, "manage-forced-colors-1280", full_page=False)
    return {"item_border": item_border, "row_border": row_border, "ring": ring}


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
        page.evaluate(
            "getComputedStyle(document.querySelector('.contacts-table tbody tr'))"
            ".display"
        )
    )
    assert row_display == "block"
    height = float(
        page.evaluate(
            "document.querySelector('.contacts-item-actions .button')"
            ".getBoundingClientRect().height"
        )
    )
    assert height >= MIN_TARGET_PX, height
    captures = [_capture(page, root, "manage-zoom-200")]
    item = _contact_item(page, _channel_label("SMS"))
    with page.expect_navigation():
        item.locator("button", has_text=gettext("Edit destination")).click()
    expect(page.locator("h1")).to_contain_text(gettext("Edit"))
    assert _no_overflow(page)
    captures.append(_capture(page, root, "edit-zoom-200"))
    return {
        **metrics,
        "window_width": ZOOM_WINDOW,
        "row_display": row_display,
        "button_height": height,
        "captures": captures,
    }


def test_reflow_forced_colors_reduced_motion_and_zoom_keep_contacts_usable(
    contacts_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    contacts_staff: dict[str, str],
) -> None:
    """The missing matrix cells: 320px reflow, forced colors, reduced
    motion and a 1280px window at 200% zoom on the populated screen."""
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
        context = contacts_browser.new_context(locale="pt-BR", **options)
        page = context.new_page()
        page.set_default_timeout(20_000)
        try:
            _sign_in(page, renewal_base_url, contacts_staff)
            _open_contacts(page, renewal_base_url, contacts_staff, PATIENT_C)
            if scene == "widths":
                report[scene] = _reflow(page, root)
            elif scene == "forced_colors":
                report[scene] = _forced_colors(page, root)
            elif scene == "zoom_200":
                report[scene] = _zoom_200(page, root)
            else:
                transition = page.evaluate(
                    "getComputedStyle(document.querySelector("
                    "'.contacts-item-actions .button')).transitionDuration"
                )
                assert transition == "0s"
                report[scene] = {"button_transition": transition}
        finally:
            context.close()
    destination = root / "contacts" / "accessibility-report.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)
