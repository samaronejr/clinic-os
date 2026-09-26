"""Real payment screens in Chromium: instructions, bounded refresh, receipts.

Every wait subscribes to a navigation, a response or a DOM state; no test here
sleeps. The suite drives the real staff and patient routes against the real
``clinic_app`` runtime, so the only way a screen reaches "Pago" is a
settlement the database already stored.

Evidence grid: the journey runs once per matrix width (1280, 768, 375) and
names every capture ``<state>-<width>``, covering the empty ledger, the draft,
the live code, a refresh in flight, the patient instructions, the denied
cross-patient link, the expired code, its regeneration, a payment event
awaiting verification, a lost connection, the receipt and the populated
ledger. The preferences test adds 320px reflow, forced colors, reduced motion,
a 1280px window at 200% zoom and the keyboard path; the native test repeats
the staff and patient flow with JavaScript disabled.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from playwright.sync_api import ViewportSize, expect

from renewal.browser._page_wait import click_when_hittable
from renewal.browser._protected import encrypt
from renewal.browser.engines import (
    grant_clipboard,
    history_back,
    history_reload,
    pasted_clipboard,
    restores_forms_on_back,
    zoom_200,
)
from renewal.browser.test_availability import availability_staff
from renewal.browser.test_encounter import press, press_in_view
from renewal.browser.test_patient_access import (
    MIN_TARGET_PX,
    _no_overflow,
    _overflowing,
    _redeem,
    _ring,
    _tab_until,
    _watch_errors,
)
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

MATRIX_WIDTHS: Final = (1280, 768, 375)
AMOUNT: Final = "180,00"
AMOUNT_TEXT: Final = "R$ 180,00"
LONG_AMOUNT: Final = "1234567,89"
LONG_AMOUNT_TEXT: Final = "R$ 1.234.567,89"
LONG_NAME: Final = "Maria Aparecida de Albuquerque Wanderley Sintetica dos Santos"
FORBIDDEN: Final = 403
OK: Final = 200
MIN_LEDGER_ROWS: Final = 3
CODE_PREFIX: Final = "SYNTHETIC-NOT-PAYABLE"
STATUS_PATTERN: Final = re.compile(r"/status/")
# The deliberate transport failures this suite causes and asserts itself: the
# refused cross-patient link, the aborted refresh, and htmx's own log of that
# aborted request. Anything else is a defect.
EXPECTED_CONSOLE: Final = frozenset(
    {
        "Failed to load resource: the server responded with a status of 403 "
        "(Forbidden)",
        "Failed to load resource: net::ERR_FAILED",
        "htmx:sendError, [object HTMLElement]",
        "htmx:afterRequest, [object HTMLElement]",
    }
)


def capture(page: Page, root: Path, state: str, width: int) -> None:
    """Store one privacy-safe capture and prove the width holds no overflow."""
    folder = root / "billing"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert _no_overflow(page), _overflowing(page)


def seed_patient(staff: dict[str, str], name: str) -> dict[str, str]:
    """Create one synthetic enrolled patient with a billing invitation code."""
    data = {key: str(uuid4()) for key in ("patient", "enrollment", "grant")}
    data["code"] = secrets.token_urlsafe(32)
    data["name"] = name
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patient "
            "(id,organization_id,full_name,birth_date,created_at) "
            "VALUES (%s,%s,%s,%s,now())",
            [
                data["patient"],
                staff["organization"],
                encrypt(conn, "intake.patient.full_name", name.encode()),
                encrypt(conn, "intake.patient.birth_date", b"1990-01-01"),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientclinicenrollment "
            "(id,organization_id,clinic_id,patient_id,idempotency_key,"
            "create_fingerprint,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,now())",
            [
                data["enrollment"],
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientaccessgrant "
            "(id,organization_id,clinic_id,patient_id,enrollment_id,issued_by_id,"
            "issued_by_label,secret_hash,operations,expires_at,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'Synthetic',%s,"
            "ARRAY['enrollment_view','billing'],now()+interval '24 hours',now())",
            [
                data["grant"],
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                data["enrollment"],
                staff["receptionist_id"],
                hashlib.sha256(data["code"].encode()).digest(),
            ],
        )
    return data


def _runtime(conn: object, staff: dict[str, str], actor: str) -> None:
    """Act exactly as the runtime role and one staff actor, never as owner."""
    execute = conn.execute  # type: ignore[attr-defined]
    execute("SET ROLE clinic_app")
    execute(
        "SELECT set_config('app.current_tenant', %s, true), "
        "set_config('app.current_user_id', %s, true)",
        [staff["organization"], actor],
    )


def seed_expired_code(staff: dict[str, str], actor: str, invoice_id: str) -> str:
    """Store one already expired request with its exact synthetic result."""
    operation = str(uuid4())
    with psycopg.connect(staff["dsn"]) as conn:
        _runtime(conn, staff, actor)
        conn.execute(
            "INSERT INTO clinic_app.billing_pixoperation "
            "(id,organization_id,invoice_id,previous_id,invoice_reference,"
            "amount_minor,currency,provider,actor_id,created_at,expires_at) "
            "SELECT %s,i.organization_id,i.id,NULL,i.reference,i.amount_minor,"
            "i.currency,'synthetic-pix-v1',%s,now()-interval '2 hours',"
            "now()-interval '1 hour' FROM clinic_app.billing_invoice i "
            "WHERE i.id = %s",
            [operation, actor, invoice_id],
        )
        conn.execute(
            "INSERT INTO clinic_app.billing_pixcharge "
            "(operation_id,organization_id,invoice_id,provider_reference,copy_code,"
            "qr_base64,synthetic,created_at) "
            "SELECT %s,o.organization_id,o.invoice_id,'synthetic-pix-' || o.id::text,"
            "%s,%s,true,now() "
            "FROM clinic_app.billing_pixoperation o WHERE o.id = %s",
            [
                operation,
                encrypt(
                    conn,
                    "billing.pixcharge.copy_code",
                    f"{CODE_PREFIX}|expirado".encode(),
                ),
                encrypt(conn, "billing.pixcharge.qr_base64", b"iVBORw0KGgo="),
                operation,
            ],
        )
    return operation


def seed_unverified_event(staff: dict[str, str], actor: str, invoice_id: str) -> None:
    """Record one authentic event the authoritative check could not confirm."""
    with psycopg.connect(staff["dsn"]) as conn:
        _runtime(conn, staff, actor)
        conn.execute(
            "INSERT INTO clinic_app.billing_paymentevent "
            "(id,organization_id,operation_id,invoice_id,settlement_id,provider,"
            "event_id,provider_reference,actor_id,reported_status,"
            "reported_amount_minor,reported_currency,authoritative_status,"
            "authoritative_amount_minor,authoritative_currency,resolution,"
            "reason_code,received_at) "
            "SELECT %s,o.organization_id,o.id,o.invoice_id,NULL,o.provider,%s,"
            "'synthetic-pix-' || o.id::text,o.actor_id,'settled',o.amount_minor,"
            "o.currency,'pending',o.amount_minor,o.currency,'operator_required',"
            "'unverified_settlement',now() "
            "FROM clinic_app.billing_pixoperation o "
            "WHERE o.invoice_id = %s AND NOT EXISTS ("
            "SELECT 1 FROM clinic_app.billing_pixoperation n "
            "WHERE n.previous_id = o.id) "
            "ORDER BY o.created_at DESC LIMIT 1",
            [str(uuid4()), f"synthetic-{uuid4().hex}", invoice_id],
        )


def ledger_url(base: str, staff: dict[str, str]) -> str:
    """Return the staff charge ledger of the seeded clinic."""
    return f"{base}/billing/clinics/{staff['clinic_a']}/charges/"


def create_charge(page: Page, url: str, patient_id: str, amount: str) -> str:
    """Create one draft through the real form and return its charge URL."""
    page.goto(url)
    page.select_option("#id_patient_id", value=patient_id)
    page.locator("#id_amount").fill(amount)
    press(page, "create")
    expect(page.locator("[data-payment-state]")).to_have_attribute(
        "data-payment-state", "draft"
    )
    return page.url


def repeat_charge(page: Page, charge_url: str, amount: str) -> str:
    """Open the deliberate second charge offered by one charge's own screen."""
    page.goto(charge_url)
    with page.expect_navigation():
        click_when_hittable(page.locator("[data-repeat-charge]"))
    expect(page.locator("#id_amount")).to_have_value(amount)
    press_in_view(page, "create")
    expect(page.locator("[data-payment-state]")).to_have_attribute(
        "data-payment-state", "draft"
    )
    return page.url


def expect_state(page: Page, state: str) -> None:
    """Assert the rendered payment state without waiting on a timer."""
    expect(page.locator("[data-payment-state]")).to_have_attribute(
        "data-payment-state", state
    )


def issue_and_code(page: Page) -> None:
    """Freeze the reviewed draft and generate its payment code."""
    press(page, "issue")
    expect_state(page, "issued")
    press(page, "code")
    expect_state(page, "pending")


def confirm_settlement(page: Page, amount: str) -> None:
    """Attest one externally verified settlement through the real form."""
    page.locator("#id_confirmation_reference").fill(str(uuid4()))
    page.locator("#settlement-form #id_amount").fill(amount)
    page.locator("#id_attested").check()
    press(page, "confirm")


def invoice_id_of(url: str) -> str:
    """Read the charge identifier out of its staff URL."""
    return url.rstrip("/").rsplit("/", maxsplit=1)[-1]


def qr_renders(page: Page) -> bool:
    """Report whether the stored QR bytes actually decoded into an image."""
    return bool(
        page.locator("[data-qr]").evaluate(
            "(image) => image.complete && image.naturalWidth > 0"
            " && image.naturalHeight > 0"
        )
    )


def await_refresh(page: Page) -> int:
    """Wait for the next bounded refresh by subscribing to its response."""
    with page.expect_response(
        lambda response: STATUS_PATTERN.search(response.url) is not None
    ) as refreshed:
        expect(page.locator("[data-poll]")).to_have_count(1)
    return int(refreshed.value.status)


def console_report(
    root: Path,
    width: int,
    streams: dict[str, list[str]],
    facts: dict[str, object],
) -> None:
    """Fail on any console error this suite did not deliberately cause."""
    unexpected = {
        name: [message for message in messages if message not in EXPECTED_CONSOLE]
        for name, messages in streams.items()
    }
    expected = {
        name: [message for message in messages if message in EXPECTED_CONSOLE]
        for name, messages in streams.items()
    }
    folder = root / "billing"
    folder.mkdir(exist_ok=True, mode=0o700)
    (folder / f"accessibility-console-{width}.json").write_text(
        json.dumps(
            {
                "console_errors": unexpected,
                "expected_transport_failures": expected,
                **facts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    assert not any(unexpected.values()), unexpected


@pytest.mark.parametrize("width", MATRIX_WIDTHS)
def test_charge_instructions_refresh_and_receipt_stay_exact(  # noqa: PLR0915 - one real journey
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    staff = availability_staff
    base = renewal_base_url
    root = renewal_artifact_root
    manager = seed_manager(staff)
    payer = seed_patient(staff, f"Paciente Sintetico Cobranca {width}")
    stranger = seed_patient(staff, LONG_NAME)
    viewport: ViewportSize = {"width": width, "height": 900}
    with (
        browser.new_context(viewport=viewport, locale="pt-BR") as staff_context,
        browser.new_context(viewport=viewport, locale="pt-BR") as payer_context,
        browser.new_context(viewport=viewport, locale="pt-BR") as stranger_context,
    ):
        grant_clipboard(staff_context)
        admin = staff_context.new_page()
        patient = payer_context.new_page()
        other = stranger_context.new_page()
        errors = _watch_errors(admin)
        patient_errors = _watch_errors(patient)
        other_errors = _watch_errors(other)

        sign_in_manager(admin, base, staff, manager)
        # The ledger is reachable from the shell, not only by typed URL.
        with admin.expect_navigation():
            admin.locator("a[data-module=billing]").click()
        ledger = ledger_url(base, staff)
        assert admin.url == ledger
        expect(admin.locator("a[data-module=billing]")).to_have_attribute(
            "aria-current", "page"
        )
        # A long synthetic name and a grouped BRL value stress the ledger.
        long_charge = create_charge(admin, ledger, stranger["patient"], LONG_AMOUNT)
        expect(admin.locator("[data-amount]")).to_contain_text(LONG_AMOUNT_TEXT)
        capture(admin, root, "charge-draft", width)

        charge_url = create_charge(admin, ledger, payer["patient"], AMOUNT)
        invoice_id = invoice_id_of(charge_url)
        issue_and_code(admin)
        assert qr_renders(admin)
        code = str(admin.locator("[data-code]").text_content())
        assert code.startswith(CODE_PREFIX)
        capture(admin, root, "charge-pending", width)

        # The copy control appears only with a clipboard, and copies exactly.
        copy_button = admin.locator("[data-copy]")
        expect(copy_button).to_be_visible()
        admin.bring_to_front()
        copy_button.click()
        expect(admin.locator("[data-copy-status]")).to_have_text("Código copiado.")
        assert pasted_clipboard(admin) == code

        # A bounded refresh re-reads stored state and never invents success.
        assert await_refresh(admin) == OK
        expect_state(admin, "pending")
        expect(admin.locator("[data-poll]")).to_have_attribute("data-poll", "2")
        capture(admin, root, "charge-refreshed", width)

        press(admin, "release")
        expect(admin.locator("[data-released]")).to_have_attribute(
            "data-released", "true"
        )

        # The patient obtains the instructions, and only their own.
        _redeem(patient, base, staff["clinic_a"], payer["code"])
        with patient.expect_navigation():
            patient.locator("#charges-link").click()
        expect(patient.locator("[data-charge]")).to_have_count(1)
        expect(patient.locator("[data-amount]")).to_have_text(AMOUNT_TEXT)
        capture(patient, root, "patient-ledger", width)
        patient.locator("[data-charge] a").click()
        expect_state(patient, "pending")
        assert str(patient.locator("[data-code]").text_content()) == code
        assert qr_renders(patient)
        capture(patient, root, "patient-instructions", width)

        _redeem(other, base, staff["clinic_a"], stranger["code"])
        with other.expect_navigation():
            other.locator("#charges-link").click()
        expect(other.locator("#charges-empty")).to_be_visible()
        capture(other, root, "patient-empty", width)
        denied = other.goto(f"{base}/patient/charges/{invoice_id}/")
        assert denied is not None
        assert denied.status == FORBIDDEN
        denied_body = str(other.content())
        assert "R$" not in denied_body
        assert CODE_PREFIX not in denied_body
        capture(other, root, "patient-denied", width)

        # A lost connection stops the chain and says the screen may be stale.
        patient.route(STATUS_PATTERN, lambda route: route.abort())
        history_reload(patient)
        expect(patient.locator("[data-offline]")).to_be_visible()
        expect_state(patient, "pending")
        capture(patient, root, "patient-offline", width)
        patient.unroute(STATUS_PATTERN)

        # Only a stored settlement turns the screens to paid.
        admin.goto(charge_url)
        confirm_settlement(admin, AMOUNT)
        expect_state(admin, "paid")
        receipt = str(admin.locator("[data-receipt]").get_attribute("data-receipt"))
        capture(admin, root, "charge-paid", width)
        patient.goto(f"{base}/patient/charges/{invoice_id}/")
        expect_state(patient, "paid")
        expect(patient.locator("[data-receipt]")).to_have_attribute(
            "data-receipt", receipt
        )
        assert CODE_PREFIX not in str(patient.content())
        capture(patient, root, "patient-receipt", width)

        # An expired code is refused, regenerated once, and never paid. This
        # second charge repeats the paid one deliberately, through the screen.
        expired_url = repeat_charge(admin, charge_url, AMOUNT)
        expired_id = invoice_id_of(expired_url)
        assert expired_id != invoice_id
        press(admin, "issue")
        press(admin, "release")
        seed_expired_code(staff, manager["id"], expired_id)
        admin.goto(expired_url)
        expect_state(admin, "expired")
        assert CODE_PREFIX not in str(admin.content())
        capture(admin, root, "charge-expired", width)
        patient.goto(f"{base}/patient/charges/{expired_id}/")
        expect_state(patient, "expired")
        assert CODE_PREFIX not in str(patient.content())
        capture(patient, root, "patient-expired", width)

        admin.goto(expired_url)
        press(admin, "regenerate")
        expect_state(admin, "pending")
        renewed = str(admin.locator("[data-code]").text_content())
        assert renewed != code
        capture(admin, root, "charge-regenerated", width)

        # An authentic event the provider check could not confirm never pays.
        seed_unverified_event(staff, manager["id"], expired_id)
        admin.goto(expired_url)
        expect_state(admin, "flagged")
        expect(admin.locator("[data-payment-reason]")).to_be_visible()
        capture(admin, root, "charge-flagged", width)
        patient.goto(f"{base}/patient/charges/{expired_id}/")
        expect_state(patient, "flagged")
        assert "Pago" not in str(patient.content())
        capture(patient, root, "patient-flagged", width)

        # A cancelled charge stops offering the code nobody should pay.
        admin.goto(expired_url)
        press_in_view(admin, "cancel")
        expect_state(admin, "cancelled")
        expect(admin.locator("[data-code]")).to_have_count(0)
        expect(admin.locator("[data-qr]")).to_have_count(0)
        capture(admin, root, "charge-cancelled", width)
        patient.goto(f"{base}/patient/charges/{expired_id}/")
        expect_state(patient, "cancelled")
        expect(patient.locator("[data-code]")).to_have_count(0)
        expect(patient.locator("[data-qr]")).to_have_count(0)
        assert CODE_PREFIX not in str(patient.content())
        capture(patient, root, "patient-cancelled", width)

        # The populated ledger keeps every state and the long name readable.
        admin.goto(ledger)
        assert admin.locator("[data-charge]").count() >= MIN_LEDGER_ROWS
        expect(admin.locator(f'[data-charge="{invoice_id}"]')).to_have_attribute(
            "data-state", "paid"
        )
        expect(
            admin.locator(f'[data-charge="{invoice_id_of(long_charge)}"]')
        ).to_have_attribute("data-state", "draft")
        capture(admin, root, "ledger", width)
        console_report(
            root,
            width,
            {
                "staff": errors,
                "patient": patient_errors,
                "other_patient": other_errors,
            },
            {
                "cancelled_code_hidden": True,
                "clipboard_copy_exact": True,
                "cross_patient_status": FORBIDDEN,
                "expired_code_hidden": True,
                "offline_refresh_warns": True,
                "qr_rendered": True,
                "receipt_reference_matches": True,
                "unverified_event_never_paid": True,
            },
        )


def _resubmit_restored_form(
    page: Page, root: Path, patient_id: str, charge_url: str
) -> None:
    """Submitting a Back-restored form opens nothing: it is the same charge.

    The screen says so instead of claiming a second creation.
    """
    expect(page.locator("#id_patient_id")).to_have_value(patient_id)
    expect(page.locator("#id_amount")).to_have_value(AMOUNT)
    capture(page, root, "back-restored", 1280)
    press(page, "create")
    assert page.url == charge_url
    expect_state(page, "draft")
    expect(page.locator("p.feedback[role=status]")).to_have_count(1)
    capture(page, root, "back-resubmitted", 1280)


def test_browser_back_and_resubmit_never_opens_a_second_charge(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """Back restores the create form; resubmitting it opens no charge.

    Firefox does not restore form controls on Back under Playwright
    (engines.restores_forms_on_back); there Back shows a fresh form, and the
    rest of the journey is the same on every engine.

    The browser, not a captured POST dictionary, decides what a restored form
    carries: it puts back the selected patient and the typed value while the
    server renders the page again. Both submissions must therefore name the
    same create operation, and the deliberate repeat must stay distinct.
    """
    browser = renewal_page.context.browser
    assert browser is not None
    staff = availability_staff
    base = renewal_base_url
    root = renewal_artifact_root
    manager = seed_manager(staff)
    name = "Paciente Sintetico Historico"
    payer = seed_patient(staff, name)
    with browser.new_context(
        viewport={"width": 1280, "height": 900}, locale="pt-BR"
    ) as context:
        page = context.new_page()
        errors = _watch_errors(page)
        sign_in_manager(page, base, staff, manager)
        ledger = ledger_url(base, staff)
        rows = page.locator("[data-charge]").filter(has_text=name)
        charge_url = create_charge(page, ledger, payer["patient"], AMOUNT)

        # Refresh the charge that was created, then press Back (engines.py,
        # Session history: the document's own Reload/Back on every engine).
        history_reload(page)
        assert page.url == charge_url
        history_back(page, ledger)
        if restores_forms_on_back(context):
            _resubmit_restored_form(page, root, payer["patient"], charge_url)
        else:
            # Back renders a fresh form here: nothing restored to resubmit.
            expect(page.locator("#id_amount")).to_have_value("")
            capture(page, root, "back-fresh-form", 1280)
        page.goto(ledger)
        expect(rows).to_have_count(1)

        # Charging the same value again is explicit, distinct and retry-safe.
        page.goto(charge_url)
        capture(page, root, "charge-repeat-offer", 1280)
        with page.expect_navigation():
            page.locator("[data-repeat-charge]").click()
        repeat_form = page.url
        expect(page.locator("#id_patient_id")).to_have_value(payer["patient"])
        expect(page.locator("#id_amount")).to_have_value(AMOUNT)
        capture(page, root, "repeat-form", 1280)

        press(page, "create")
        second_url = page.url
        assert second_url != charge_url
        expect_state(page, "draft")
        history_back(page, repeat_form)
        press(page, "create")
        assert page.url == second_url
        page.goto(ledger)
        expect(rows).to_have_count(2)
        capture(page, root, "repeat-created", 1280)
    assert not errors


def test_preferences_keyboard_and_reflow_hold_on_the_payment_screen(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    staff = availability_staff
    base = renewal_base_url
    root = renewal_artifact_root
    manager = seed_manager(staff)
    payer = seed_patient(staff, "Paciente Sintetico Preferencias")
    with browser.new_context(
        viewport={"width": 375, "height": 900}, locale="pt-BR"
    ) as context:
        grant_clipboard(context)
        page = context.new_page()
        errors = _watch_errors(page)
        sign_in_manager(page, base, staff, manager)
        charge_url = create_charge(
            page, ledger_url(base, staff), payer["patient"], AMOUNT
        )
        issue_and_code(page)

        # Keyboard: every stop keeps a visible ring and a 44px target.
        focused: list[str] = []
        copy_button = page.locator("[data-copy]")
        page.locator("#main-content").focus()
        _tab_until(page, copy_button, focused)
        assert copy_button.evaluate("(e) => e.getBoundingClientRect().height") >= (
            MIN_TARGET_PX
        )
        ring = _ring(page)
        assert ring["style"] == "solid"
        page.bring_to_front()
        page.keyboard.press("Enter")
        expect(page.locator("[data-copy-status]")).to_have_text("Código copiado.")
        capture(page, root, "keyboard-copy", 375)

        page.set_viewport_size({"width": 320, "height": 900})
        capture(page, root, "reflow", 320)
        page.emulate_media(forced_colors="active", reduced_motion="reduce")
        capture(page, root, "forced-colors-reduced-motion", 320)
        page.emulate_media(forced_colors="none", reduced_motion="no-preference")

        zoom_context, zoomed = zoom_200(page)
        try:
            zoom_errors = _watch_errors(zoomed)
            zoomed.goto(charge_url)
            assert zoomed.evaluate("[devicePixelRatio, innerWidth]") == [2, 640]
            capture(zoomed, root, "zoom-200-layout", 640)
            assert not zoom_errors
        finally:
            zoom_context.close()
    assert not errors


def test_native_flow_completes_without_javascript(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    staff = availability_staff
    base = renewal_base_url
    root = renewal_artifact_root
    manager = seed_manager(staff)
    payer = seed_patient(staff, "Paciente Sintetico Sem Script")
    viewport: ViewportSize = {"width": 768, "height": 900}
    with (
        browser.new_context(
            viewport=viewport, locale="pt-BR", java_script_enabled=False
        ) as staff_context,
        browser.new_context(
            viewport=viewport, locale="pt-BR", java_script_enabled=False
        ) as patient_context,
    ):
        admin = staff_context.new_page()
        patient = patient_context.new_page()
        errors = _watch_errors(admin)
        patient_errors = _watch_errors(patient)
        sign_in_manager(admin, base, staff, manager)
        charge_url = create_charge(
            admin, ledger_url(base, staff), payer["patient"], AMOUNT
        )
        issue_and_code(admin)
        # Without JavaScript the copy button stays hidden and the code stays
        # selectable; the manual refresh link replaces the polling chain.
        expect(admin.locator("[data-code]")).to_be_visible()
        expect(admin.locator("[data-copy]")).to_be_hidden()
        with admin.expect_navigation():
            admin.locator("[data-refresh]").click()
        expect_state(admin, "pending")
        capture(admin, root, "native-pending", 768)

        press(admin, "release")
        _redeem(patient, base, staff["clinic_a"], payer["code"])
        patient.goto(f"{base}/patient/charges/{invoice_id_of(charge_url)}/")
        expect_state(patient, "pending")
        expect(patient.locator("[data-copy]")).to_be_hidden()
        capture(patient, root, "native-patient-instructions", 768)

        admin.goto(charge_url)
        confirm_settlement(admin, AMOUNT)
        expect_state(admin, "paid")
        with patient.expect_navigation():
            patient.locator("[data-refresh]").click()
        expect_state(patient, "paid")
        expect(patient.locator("[data-receipt]")).to_have_count(1)
        capture(patient, root, "native-patient-receipt", 768)
    assert not errors
    assert not patient_errors
