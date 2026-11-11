"""Todo 72 resilience suite: core work continues while providers are down.

The runner serves this suite with ``CLINIC_SYNTHETIC_FAULTS=payment=down,
signing=down``, the two provider families whose calls run inside a staff
request. The physician still opens the visit and saves the note, and the
signer's outage leaves one retryable, unissued operation; finance still issues
a charge and records a manually confirmed settlement while no payment code can
be generated. The worker-side families (AI, video and messaging) and the slow
and ambiguous modes run through the real outbox in ``tests/resilience``.
Every wait subscribes to a navigation or a DOM state; nothing sleeps.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser._navigation import goto_settled
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_billing import (
    AMOUNT,
    _runtime,
    confirm_settlement,
    create_charge,
    expect_state,
    ledger_url,
    seed_patient,
)
from renewal.browser.test_encounter import FIELDS, press, press_in_view, seed, stored
from renewal.browser.test_patient_access import _no_overflow, _overflowing
from renewal.browser.test_prescribing import (
    author_and_render,
    ensure_physician_profile,
    operations,
)
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

WIDTHS: Final = (1280, 375)
DAYS: Final = {1280: "2035-09-03", 375: "2035-09-04"}
NOTE: Final = "Relato sintético registrado durante a falha do provedor"
PIX_UNAVAILABLE: Final = (
    "A geração de código de pagamento não está disponível neste ambiente. "
    "Nenhuma cobrança foi criada."
)


def capture(page: Page, root: Path, state: str, width: int) -> None:
    """Store one privacy-safe capture and prove the width holds no overflow."""
    folder = root / "resilience"
    folder.mkdir(exist_ok=True, mode=0o700)
    full_page_screenshot(page, folder / f"{state}-{width}.png")
    assert _no_overflow(page), _overflowing(page)


def payment_codes(
    staff: dict[str, str], actor: str, invoice_id: str
) -> tuple[int, int]:
    """Count stored code requests and codes as the runtime role; none may exist."""
    with psycopg.connect(staff["dsn"]) as conn:
        _runtime(conn, staff, actor)
        row = conn.execute(
            "SELECT (SELECT count(*) FROM clinic_app.billing_pixoperation "
            "WHERE invoice_id = %s), (SELECT count(*) FROM "
            "clinic_app.billing_pixcharge WHERE invoice_id = %s)",
            [invoice_id, invoice_id],
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def report(root: Path, name: str, value: dict[str, object]) -> None:
    folder = root / "resilience"
    folder.mkdir(exist_ok=True, mode=0o700)
    (folder / f"{name}.json").write_text(json.dumps(value, sort_keys=True) + "\n")


@pytest.mark.parametrize("width", WIDTHS)
def test_the_note_is_saved_and_a_signer_outage_stays_retryable(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    data = seed(staff, DAYS[width])
    ensure_physician_profile(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _sign_in_physician(page, base, staff)
        # Charting needs no provider: open the arrived visit and save the note.
        goto_settled(
            page,
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{DAYS[width]}/1/",
        )
        press(page, "open")
        page.locator("#template-id").select_option(data["specialty"])
        press(page, "template")
        version = page.locator("[data-version]").get_attribute("data-version")
        assert version is not None
        for field in FIELDS:
            page.locator(f"#id_{field}").fill(
                NOTE if field == "subjective" else f"Registro sintético {field}"
            )
        press(page, "save")
        expect(page.locator("#save-state")).to_have_attribute("data-state", "saved")
        assert stored(staff, version) == (2, NOTE)
        capture(page, root, "note-saved", width)
        # The signer is down: the frozen document is not issued and the
        # operation waits, visibly, for a retry that cannot succeed yet.
        document = author_and_render(page, base, staff, DAYS[width])
        press_in_view(page, "sign_document")
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "prepared")
        expect(page.locator('button[value="retry_dispatch"]')).to_be_visible()
        assert operations(staff, document) == [("prepared", "")]
        capture(page, root, "signer-down", width)
        press(page, "retry_dispatch")
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "prepared")
        assert operations(staff, document) == [("prepared", "")]
        assert not errors
        report(
            root,
            f"signing-{width}",
            {"note_revision": 2, "operations": operations(staff, document)},
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_a_payment_outage_still_allows_a_manual_settlement(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    manager = seed_manager(staff)
    payer = seed_patient(staff, f"Paciente Sintetico Resiliencia {width}")
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    admin = context.new_page()
    errors: list[str] = []
    admin.on("pageerror", lambda error: errors.append(str(error)))
    try:
        sign_in_manager(admin, base, staff, manager)
        charge_url = create_charge(
            admin, ledger_url(base, staff), payer["patient"], AMOUNT
        )
        invoice_id = charge_url.rstrip("/").rsplit("/", maxsplit=1)[-1]
        press(admin, "issue")
        expect_state(admin, "issued")
        # The payment provider is down: the code request is refused honestly.
        with admin.expect_response(
            lambda response: response.request.method == "POST"
        ) as refused:
            press(admin, "code")
        assert refused.value.status == 503
        expect(admin.locator("main")).to_contain_text(PIX_UNAVAILABLE)
        capture(admin, root, "payment-refused", width)
        goto_settled(admin, charge_url)
        expect_state(admin, "issued")
        assert payment_codes(staff, manager["id"], invoice_id) == (0, 0)
        capture(admin, root, "payment-down", width)
        # Ordinary billing continues: finance confirms a payment received by
        # other means, and the charge settles without any provider.
        confirm_settlement(admin, AMOUNT)
        expect_state(admin, "paid")
        capture(admin, root, "manual-settlement", width)
        assert payment_codes(staff, manager["id"], invoice_id) == (0, 0)
        assert not errors
        report(root, f"payment-{width}", {"refused_status": 503, "state": "paid"})
    finally:
        context.close()
