"""Render every provider's degraded state and manual path, plus governance.

Run this suite with each CLINIC_RENEWAL_RESILIENCE_MODE: down, slow, ambiguous.
Requests use the served runtime and workers use clinic_app on the same database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from django.utils.translation import gettext, pgettext
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import worker_dsn
from renewal.browser._navigation import click_to_navigate, expect_document, goto_settled
from renewal.browser._page_wait import evaluate_js
from renewal.browser._teleconsult import register_physician
from renewal.browser.engines import full_page_screenshot
from renewal.browser.messages_support import (
    latest_operation,
    message_body,
    seed_provider,
)
from renewal.browser.support_managers import seed_ai_admin
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_billing import (
    AMOUNT,
    _runtime,
    confirm_settlement,
    create_charge,
    expect_state,
    ledger_url,
    seed_patient,
)
from renewal.browser.test_clinical_inbox import (
    _arrived_today,
    _brief_command,
    _brief_post,
)
from renewal.browser.test_document_verification import _operation_row
from renewal.browser.test_encounter import FIELDS, press, press_in_view, seed, stored
from renewal.browser.test_messages import _inbox, _open_thread, _reply
from renewal.browser.test_patient_access import _no_overflow, _overflowing, _redeem
from renewal.browser.test_prescribing import (
    author_and_render,
    ensure_physician_profile,
    operations,
)
from renewal.browser.test_realtime import _today_scene
from renewal.browser.test_retention import seed_manager, sign_in_manager
from renewal.browser.test_teleconsult import (
    _accept_consent,
    _open_encounter,
    _publish_consent,
    _room_operation,
)
from renewal.browser.test_teleconsult import _seed as seed_video

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

WIDTHS: Final = (1280, 375)
DAYS: Final = {1280: "2035-09-03", 375: "2035-09-04"}
NOTE: Final = "Relato sintético registrado durante a falha do provedor"
MODE: Final = os.environ.get("CLINIC_RENEWAL_RESILIENCE_MODE", "down")
PIX_UNAVAILABLE: Final = (
    "A geração de código de pagamento não está disponível neste ambiente. "
    "Nenhuma cobrança foi criada."
)


def capture(page: Page, root: Path, state: str, width: int) -> None:
    """Store one privacy-safe capture and prove the width holds no overflow."""
    folder = root / "resilience"
    folder.mkdir(exist_ok=True, mode=0o700)
    evaluate_js(page, "scrollTo(0, 0)")
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


def run_provider_worker(operation: str, root: Path) -> list[str]:
    """Execute the browser's stored operation as the runtime database role."""
    environment = {
        **os.environ,
        "APP_DATABASE_URL": worker_dsn(),
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "PYTHONPATH": os.pathsep.join((".", "tests")),
    }
    result = subprocess.run(  # noqa: S603 - fixed worker, owned DSN, UUID argument
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from renewal.resilience_worker import main; main()",
            operation,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    (root / f"resilience-worker-{operation}.log").write_text(
        result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stderr
    result_data = json.loads(result.stdout)
    assert result_data["runtime_role"] == "clinic_app"
    return list(result_data["outcomes"])


def save_manual_note(page: Page, staff: dict[str, str], specialty: str) -> None:
    page.locator("#template-id").select_option(specialty)
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
        save_manual_note(page, staff, data["specialty"])
        capture(page, root, "note-saved", width)
        # The signer is down: the frozen document is not issued and the
        # operation waits, visibly, for a retry that cannot succeed yet.
        document = author_and_render(page, base, staff, DAYS[width])
        press_in_view(page, "sign_document")
        state = "failed" if MODE == "ambiguous" else "prepared"
        failure = "invalid_provider_reference" if MODE == "ambiguous" else ""
        expect(page.locator("[data-state]")).to_have_attribute("data-state", state)
        assert operations(staff, document) == [(state, failure)]
        assert _operation_row(staff, document)["operation_ref"] == ""
        capture(page, root, f"signer-{MODE}", width)
        if MODE != "ambiguous":
            expect(page.locator('button[value="retry_dispatch"]')).to_be_visible()
            press(page, "retry_dispatch")
            expect(page.locator("[data-state]")).to_have_attribute("data-state", state)
            assert operations(staff, document) == [(state, failure)]
        else:
            expect(page.locator('button[value="retry_dispatch"]')).to_have_count(0)
        assert not errors
        report(
            root,
            f"signing-{width}",
            {"note_revision": 2, "operations": operations(staff, document)},
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_video_failure_keeps_the_manual_chart_usable(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    day = {1280: "2037-09-03", 375: "2037-09-04"}[width]
    note = seed(staff, day)
    data = seed_video(staff, day, 14)
    register_physician(staff)
    page = renewal_page
    page.set_viewport_size({"width": width, "height": 900})
    browser = page.context.browser
    assert browser is not None
    with browser.new_context(locale="pt-BR") as admin_context:
        admin = admin_context.new_page()
        sign_in_manager(admin, base, staff, seed_manager(staff))
        _publish_consent(admin, f"{base}/clinics/{staff['clinic_a']}/consent/")
    with browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    ) as patient_context:
        patient = patient_context.new_page()
        _redeem(patient, base, staff["clinic_a"], data["code"])
        _accept_consent(patient, base)
        capture(patient, root, f"video-consent-{MODE}", width)
    _sign_in_physician(page, base, staff)
    _open_encounter(page, base, staff, day, data["appointment"])
    save_manual_note(page, staff, note["specialty"])
    chart_url = page.url
    encounter = page.locator("[data-encounter]").get_attribute("data-encounter")
    assert encounter is not None
    staff_url = f"{base}/teleconsult/clinics/{staff['clinic_a']}/"
    goto_settled(page, staff_url)
    click_to_navigate(
        page.locator(
            f'form:has(input[name="encounter_id"][value="{encounter}"]) '
            'button[value="create"]'
        )
    )
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        created = conn.execute(
            "SELECT id FROM clinic_app.teleconsult_teleconsultsession "
            "WHERE encounter_id=%s",
            [encounter],
        ).fetchone()
    assert created is not None
    session = str(created[0])
    operation = _room_operation(staff, session)
    outcomes = run_provider_worker(operation, root)
    assert outcomes[-1] == ("reconcile" if MODE == "ambiguous" else "failed")
    goto_settled(page, staff_url)
    row = page.locator(f'article[data-session="{session}"]')
    expect(row).to_have_attribute(
        "data-state", "waiting" if MODE == "ambiguous" else "failed"
    )
    if MODE != "ambiguous":
        expect(row.locator('button[value="join"]')).to_have_count(0)
        expect(row.locator("strong")).to_have_text("Falhou")
    else:
        assert run_provider_worker(operation, root) == ["reconcile"]
        assert not row.locator("#room-name").count()
    capture(page, root, f"video-{MODE}", width)
    goto_settled(page, chart_url)
    expect(page.locator("#save-state")).to_have_attribute("data-state", "saved")
    expect(page.locator("#id_subjective")).to_have_value(NOTE)
    capture(page, root, f"video-manual-chart-{MODE}", width)


@pytest.mark.parametrize("width", WIDTHS)
def test_messaging_failure_keeps_manual_inbox_work_available(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    provider = seed_provider(staff)
    assert (
        provider.deliver(renewal_page, base, message_body("Contato sintético")).status
        == 200
    )
    page = renewal_page
    page.set_viewport_size({"width": width, "height": 900})
    _sign_in_receptionist(page, base, staff)
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    _reply(page, "Resposta sintética")
    operation, _ = latest_operation(staff)
    outcomes = run_provider_worker(operation, root)
    assert outcomes[-1] == ("reconcile" if MODE == "ambiguous" else "failed")
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    outgoing = page.locator(".thread__item--out")
    expect(outgoing).to_have_attribute(
        "data-state", "queued" if MODE == "ambiguous" else "failed"
    )
    expect(outgoing).to_contain_text(
        pgettext("delivery state", "Queued" if MODE == "ambiguous" else "Not sent")
    )
    assert latest_operation(staff)[1] is None
    capture(page, root, f"messaging-{MODE}", width)
    click_to_navigate(page.get_by_role("button", name=gettext("Assign to me")))
    expect(
        page.get_by_role("status").filter(has_text=gettext("Assignment updated."))
    ).to_be_visible()
    click_to_navigate(page.get_by_role("button", name=gettext("Mark as pending")))
    capture(page, root, f"messaging-manual-handling-{MODE}", width)


@pytest.mark.parametrize("width", WIDTHS)
def test_ai_failure_keeps_the_manual_chart_available(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = _today_scene(availability_staff)
    register_physician(staff)
    base, root = renewal_base_url, renewal_artifact_root
    data = seed(staff, {1280: "2038-09-03", 375: "2038-09-04"}[width])
    _arrived_today(staff, data["patient"])
    page = renewal_page
    page.set_viewport_size({"width": width, "height": 900})
    _sign_in_physician(page, base, staff)
    url = f"{base}/clinics/{staff['clinic_a']}/today/"
    goto_settled(page, url)
    with expect_document(page):
        page.locator(".today-next button[type=submit]").click()
    administrator = seed_ai_admin(staff, "light")
    with pytest.MonkeyPatch.context() as patch:
        # Approve the fixture model before faulting its runtime provider.
        patch.delenv("CLINIC_SYNTHETIC_FAULTS", raising=False)
        assert (
            _brief_command(
                patch,
                staff["dsn"],
                "seed",
                administrator["id"],
                staff["clinic_a"],
                staff["organization"],
            )["status"]
            == "seeded"
        )
    with psycopg.connect(staff["dsn"]) as conn:
        _runtime(conn, staff, staff["physician_a_id"])
        preflight = conn.execute(
            "SELECT clinic_app.has_permission('demographics.read',%s,NULL), "
            "clinic_app.has_permission('clinical.read',%s,%s), "
            "EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment "
            "WHERE id=%s), clinic_app.ai_organization_policy()->>'enabled', "
            "EXISTS (SELECT 1 FROM clinic_app.ai_gatewayswitch WHERE enabled)",
            [
                staff["clinic_a"],
                staff["clinic_a"],
                data["enrollment"],
                data["enrollment"],
            ],
        ).fetchone()
    assert preflight == (True, True, True, "true", True), preflight
    goto_settled(page, url)
    capture(page, root, f"ai-configured-{MODE}", width)
    expect(page.locator('#chart-panel button[value="generate"]')).to_be_visible()
    _brief_post(
        page, page.get_by_role("button", name=gettext("Generate pre-visit brief"))
    )
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        operation = conn.execute(
            "SELECT operation_id FROM clinic_app.ai_aiinvocation "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert operation is not None
    assert run_provider_worker(str(operation[0]), root) == ["failed"]
    _brief_post(page, page.get_by_role("button", name=gettext("Refresh brief")))
    expect(page.locator("#chart-panel")).to_have_attribute(
        "data-chart-state", "unavailable"
    )
    expect(page.locator("#chart-panel")).to_contain_text(gettext("Brief unavailable"))
    capture(page, root, f"ai-{MODE}", width)
    with expect_document(page):
        page.locator(".today-next button[type=submit]").click()
    save_manual_note(page, staff, data["specialty"])
    capture(page, root, f"ai-manual-chart-{MODE}", width)


@pytest.mark.parametrize("width", WIDTHS)
def test_governance_displays_the_committed_recovery_record(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    page = renewal_page
    page.set_viewport_size({"width": width, "height": 900})
    manager = seed_ai_admin(staff, "light")
    # Today stories create additional units; governance requires all of them.
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        clinics = conn.execute(
            "SELECT c.id FROM clinic_app.identity_clinic c "
            "WHERE c.organization_id=%s AND NOT EXISTS "
            "(SELECT 1 FROM clinic_app.identity_userclinicrole r "
            "WHERE r.clinic_id=c.id AND r.user_id=%s AND r.role='org_admin')",
            [staff["organization"], manager["id"]],
        ).fetchall()
        for (clinic,) in clinics:
            conn.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
                "VALUES (%s,%s,%s,%s,'org_admin',1)",
                [str(uuid4()), manager["id"], staff["organization"], str(clinic)],
            )
    sign_in_manager(page, renewal_base_url, staff, manager)
    response = goto_settled(
        page, f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/governance/"
    )
    assert response is not None
    assert response.status == 200
    slot = page.locator('[data-recovery-exercise="recorded"]')
    expect(slot).to_be_visible()
    expect(slot).to_contain_text("67d3aa4a0ab0")
    expect(slot.locator("[data-recovery-rpo]")).to_contain_text("300")
    expect(slot.locator("[data-recovery-rto]")).to_contain_text("3600")
    capture(page, renewal_artifact_root, "governance-recorded", width)


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
