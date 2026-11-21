"""Acceptance scenarios sec.12 rows 11-20 (todo 75 ``acceptance-scenarios``).

Same contract as ``test_acceptance_clinical``: rows the owning todo proved
through the real pages re-run that proof unchanged in the flagship runtime;
rows without a browser proof get new journeys here. Legs that have no HTTP
surface by design (the PSP callback adapter and its lookup-only recovery, the
governed import service, the owner's subscription receipts) run through
``renewal.flagship_fixture`` / the owner CLI exactly as the worker or operator
would, and every outcome is then read back through the patient and staff
screens. Live providers, real competitor formats, provider PITR and real SaaS
collection stay BLOCKED-ON-EG.
"""

# Injects SINTETICO-SENTINELA-* values into protected fields (runner scan).
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from ops.testing.sentinel_scan import new_sentinel
from playwright.sync_api import expect

import renewal.browser.test_messages as messages
import renewal.browser.test_portal as portal
import renewal.browser.test_prescribing as prescribing
import renewal.browser.test_results as results
import renewal.browser.test_retention as retention
import renewal.browser.test_scribe_capture as scribe
from renewal.browser._fixture_secrets import new_totp_key
from renewal.browser._guardian_support import seed_evidence
from renewal.browser._navigation import click_to_navigate, goto_settled, reload_settled
from renewal.browser._page_wait import wait_for_js
from renewal.browser._totp_devices import seed_totp_device
from renewal.browser.flagship_support import (
    REPOSITORY,
    capture,
    run_fixture,
    sign_in,
    staff_context,
)
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_availability import _sign_in_physician
from renewal.browser.test_billing import expect_state, seed_patient
from renewal.browser.test_document_verification import drive_signature
from renewal.browser.test_encounter import press_in_view
from renewal.browser.test_encounter import seed as seed_encounter
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_retention import seed_manager, sign_in_manager
from renewal.browser.test_scribe_capture import capture_page, open_capture
from renewal.browser.test_staff_intake import SEARCH, SETTLED_JS, _search

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = ("capture_page",)
OK: Final = 200
CONFLICT: Final = 409
# Each scenario owns its civil day, so no run shares a slot with another.
S12_DAY: Final = "2035-08-28"
S15_DAY: Final = "2047-03-18"
S19_DAY: Final = "2047-03-25"
QUEUED: Final = (
    "Pending audio queued for transcription. Continue manually while waiting."
)
STALE_REVIEW: Final = (
    "The draft changed after review. Render and review the saved version again."
)
KEK_FILE: Final = "tenant-kek.secret"
SUBSCRIPTIONS: Final = "/api/subscriptions/v1/"


def _browser(page: Page) -> Browser:
    browser = page.context.browser
    assert browser is not None
    return browser


def test_s11_wrong_patient_result_is_quarantined(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S11.wrong-patient-result",
        "failure",
        results.test_result_upload_acknowledgment_release_and_wrong_patient,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_s11_signed_lab_callback_replay_files_once(
    renewal_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S11.wrong-patient-result",
        "adversarial",
        results.test_signed_callback_http_replay_has_one_version_and_one_task,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        availability_staff=availability_staff,
    )


def test_s12_changed_prescription_bytes_refuse_the_stale_review(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    staff, base = availability_staff, renewal_base_url
    seed_encounter(staff, S12_DAY)
    prescribing.ensure_physician_profile(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    draft_url = f"{base}/prescription/clinics/{staff['clinic_a']}/draft/"
    try:
        review = context.new_page()
        _sign_in_physician(review, base, staff)
        # The physician reviews the exact rendered bytes (their digest).
        reviewed = prescribing.author_and_render(review, base, staff, S12_DAY)
        digest = review.locator("[data-digest]").get_attribute("data-digest")
        assert digest
        # Before any signature, the content those bytes came from changes.
        editor = context.new_page()
        goto_settled(editor, draft_url)
        editor.locator("#id_items-0-dose").fill("Dose alterada antes da assinatura")
        press_in_view(editor, "save")
        press_in_view(editor, "render_document")
        expect(editor.locator("[data-step]")).to_have_attribute("data-step", "review")
        fresh = editor.locator("[data-document]").first.get_attribute("data-document")
        assert fresh
        assert fresh != reviewed
        assert editor.locator("[data-digest]").get_attribute("data-digest") != digest
        # The open review still offers signing; requesting it is refused.
        with review.expect_response(
            lambda response: response.request.method == "POST"
        ) as refused:
            press_in_view(review, "sign_document")
        assert refused.value.status == CONFLICT
        expect(review.get_by_text(gettext(STALE_REVIEW))).to_be_visible()
        capture(review, renewal_artifact_root, "acceptance", "s12-stale-review")
        assert prescribing.operations(staff, reviewed) == []
        # Control: the already-rendered changed bytes can sign from their own review.
        press_in_view(editor, "sign_document")
        assert drive_signature(editor, staff, fresh) == "signing"
        expect(editor.locator("[data-state]")).to_have_attribute(
            "data-state", "signing"
        )
        assert prescribing.operations(staff, fresh) == [("signing", "")]
        assert prescribing.operations(staff, reviewed) == []
    finally:
        context.close()
    flagship.cover(
        "S12.prescription-bytes-changed",
        "adversarial",
        stale_sign_http=CONFLICT,
        changed_pdf_before_sign=True,
        stale_operations=0,
        rereviewed_operations=1,
    )


def _payer(staff: dict[str, str], label: str) -> tuple[dict[str, str], dict[str, str]]:
    manager = seed_manager(staff)
    payer = seed_patient(staff, f"Paciente Sintetico {label}")
    return manager, payer


def _psp_root(intent: object) -> Path:
    """A private provider-fact directory that survives a killed worker."""
    root = Path(os.environ["CLINIC_SECRET_DIR"]) / f"flagship-psp-{intent}"
    root.mkdir(mode=0o700)
    return root


def _patient_charge(
    browser: Browser, base: str, staff: dict[str, str], payer: dict[str, str]
) -> Page:
    context = browser.new_context(
        viewport={"width": 375, "height": 900},
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
    )
    page = context.new_page()
    _redeem(page, base, staff["clinic_a"], payer["code"])
    return page


def test_s13_forged_replayed_and_late_callbacks_settle_once(  # noqa: PLR0913 - scenario fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    staff = availability_staff
    manager, payer = _payer(staff, "S13")
    scope = {
        "actor": manager["id"],
        "clinic": staff["clinic_a"],
        "organization": staff["organization"],
    }
    prepared = run_fixture(
        staff["dsn"], "psp-prepare", {**scope, "patient": payer["patient"]}
    )
    root = _psp_root(prepared["intent"])
    sent = run_fixture(
        staff["dsn"],
        "psp-send",
        {"operation": str(prepared["operation"]), "root": str(root)},
    )
    assert sent["outcome"] == "succeeded"
    page = _patient_charge(_browser(renewal_page), renewal_base_url, staff, payer)
    try:
        charge = f"{renewal_base_url}/patient/charges/{prepared['invoice']}/"
        goto_settled(page, charge)
        expect_state(page, "pending")
        decisions = run_fixture(
            staff["dsn"], "psp-callbacks", {**scope, "intent": str(prepared["intent"])}
        )
        assert decisions["forged"] == "refused"
        assert decisions["tampered"] == "refused"
        assert decisions["valid"] == "verified"
        assert decisions["replayed"] == "duplicate"
        assert decisions["out_of_order"] == "superseded"
        assert decisions["verified_evidence"] == 1
        assert decisions["same_receipt"] is True
        reload_settled(page)
        expect_state(page, "paid")
        expect(page.locator("[data-receipt]")).to_have_count(1)
        capture(page, renewal_artifact_root, "acceptance", "s13-paid-once")
    finally:
        page.context.close()
    flagship.blocked(reference_clinic, "pix", "unit_a")
    flagship.blocked(reference_clinic, "psp_card", "unit_a")
    flagship.adapter(
        "psp", "synthetic", "recorded-psp-v1 fixture provider; HMAC on raw bytes"
    )
    flagship.cover(
        "S13.payment-callbacks",
        "adversarial",
        forged="refused",
        replayed="duplicate",
        out_of_order="superseded",
        settlements=1,
    )


def test_s14_worker_killed_after_the_remote_effect_never_resends(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    staff = availability_staff
    manager, payer = _payer(staff, "S14")
    scope = {
        "actor": manager["id"],
        "clinic": staff["clinic_a"],
        "organization": staff["organization"],
    }
    prepared = run_fixture(
        staff["dsn"], "psp-prepare", {**scope, "patient": payer["patient"]}
    )
    root = _psp_root(prepared["intent"])
    run_fixture(
        staff["dsn"],
        "psp-send",
        {"operation": str(prepared["operation"]), "root": str(root)},
        killed=True,
        environment={"FLAGSHIP_KILL_AFTER_EFFECT": "1"},
    )
    # The provider holds the effect; the clinic recorded nothing yet.
    assert len((root / "remote-create-attempts").read_text().splitlines()) == 1
    page = _patient_charge(_browser(renewal_page), renewal_base_url, staff, payer)
    try:
        charge = f"{renewal_base_url}/patient/charges/{prepared['invoice']}/"
        goto_settled(page, charge)
        expect(page.locator("[data-payment-state]")).not_to_have_attribute(
            "data-payment-state", "paid"
        )
        capture(page, renewal_artifact_root, "acceptance", "s14-ambiguous")
        recovered = run_fixture(
            staff["dsn"],
            "psp-recover",
            {**scope, "intent": str(prepared["intent"]), "root": str(root)},
        )
        assert recovered["decision"] == "verified"
        assert recovered["remote_creates"] == 1
        assert recovered["verified_evidence"] == 1
        assert recovered["same_receipt"] is True
        reload_settled(page)
        expect_state(page, "paid")
        capture(page, renewal_artifact_root, "acceptance", "s14-reconciled")
    finally:
        page.context.close()
    flagship.cover(
        "S14.worker-dies-after-effect",
        "failure",
        worker_exit="SIGKILL",
        remote_creates=1,
        settlements=1,
    )


def test_s15_messaging_provider_down_shows_not_sent(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S15.provider-down",
        "failure",
        messages.test_provider_down_shows_not_sent_and_recovery_never_duplicates,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_s15_ai_unavailable_keeps_manual_care(  # noqa: PLR0913 - outage proof fixtures
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    staff, page = availability_staff, capture_page
    scope = {
        "actor": staff["physician_a_id"],
        "clinic": staff["clinic_a"],
        "organization": staff["organization"],
    }
    data = open_capture(page, renewal_base_url, staff, S15_DAY)
    # The synthetic ASR provider times out while the worker transcribes.
    outage = run_fixture(
        staff["dsn"], "scribe-outage", {**scope, "appointment": data["appointment"]}
    )
    assert outage["outcomes"] == ["failed", "transcription_pending"], outage
    assert outage["reason"] == "provider_timeout"
    assert outage["open_circuits"] == 1
    # No invented text and no silent fallback; the audio is retained.
    assert outage["transcript_versions"] == 0
    assert outage["retained_chunks"] == 2
    reload_settled(page)
    transcript = page.locator('form[action$="/scribe/transcript/"] button')
    click_to_navigate(transcript)
    expect(page.locator("[data-transcript-state]")).to_have_attribute(
        "data-transcript-state", "transcription_pending"
    )
    expect(page.locator("[data-transcript-segment]")).to_have_count(0)
    capture(page, renewal_artifact_root, "acceptance", "s15-asr-down")
    # The clinical core stays usable: the note is saved by hand.
    click_to_navigate(page.locator('a[href$="/encounter/"]').first)
    scribe.save_manual_note(page)
    # Recovery: the operator closes the circuit; pending audio can queue again.
    closed = run_fixture(
        staff["dsn"],
        "scribe-circuit-reset",
        {**scope, "configuration": str(outage["configuration"])},
    )
    assert closed == {"closed": 1}
    click_to_navigate(transcript)
    click_to_navigate(page.locator('button[value="queue"]'))
    expect(page.get_by_text(gettext(QUEUED))).to_be_visible()
    flagship.cover(
        "S15.provider-down",
        "failure",
        asr_timeout=True,
        circuit_opened=1,
        fallback_admissions=0,
        invented_segments=0,
        manual_note_saved=True,
        circuit_reset=1,
    )
    for capability in ("asr", "llm_inference", "whatsapp", "sms", "email"):
        flagship.blocked(reference_clinic, capability, "unit_a")


def _session_key(page: Page) -> str:
    cookies = [c for c in page.context.cookies() if "session" in c["name"].lower()]
    assert len(cookies) == 1
    return str(cookies[0]["value"])


def test_s16_partner_migration_reconciles_and_adjudicates_conflicts(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    browser = _browser(renewal_page)
    operator = staff_context(browser, 1280)
    reception = staff_context(browser, 375)
    try:
        page = operator.new_page()
        sign_in(page, renewal_base_url, reference, "manager_a")
        scope = {
            "actor": reference.personas["manager_a"].user_id,
            "clinic": reference.unit_a,
            "organization": reference.organization,
            "session": _session_key(page),
        }
        committed = run_fixture(
            reference.dsn,
            "import-rehearsal",
            {**scope, "offset": "71600", "count": "4", "commit": "1"},
        )
        # Counts reconcile: every row is either accepted or rejected with a reason.
        assert committed["rows"] == 7
        assert committed["valid"] == 4
        assert committed["rejected"] == 3
        assert committed["reasons"] == [
            "duplicate_identifier",
            "invalid_cpf",
            "invalid_date",
        ]
        assert committed["outcomes"] == ["committed"] * int(str(committed["batches"]))
        assert committed["imported"] == 4
        reconciliation = committed["reconciliation"]
        assert isinstance(reconciliation, dict)
        assert reconciliation["source_sha256"] == committed["source_sha256"]
        assert int(str(reconciliation["source_bytes"])) > 0
        assert reconciliation["retained_source_matches"] is True
        assert reconciliation["report_digest_verified"] is True
        assert reconciliation["provenance_records_matched"] == 4
        assert reconciliation["demographics_records_matched"] == 4
        assert reconciliation["batch_hashes_match_source"] is True
        # A second partner file re-sends a patient that now exists: it is routed
        # to duplicate review, never merged or committed silently.
        again = run_fixture(
            reference.dsn,
            "import-rehearsal",
            {**scope, "offset": "71600", "count": "1", "commit": "0"},
        )
        assert again["state"] == "dry_run"
        assert int(str(again["duplicates"])) >= 1
        desk = reception.new_page()
        sign_in(desk, renewal_base_url, reference, "reception_a1")
        goto_settled(
            desk, f"{renewal_base_url}/intake/clinics/{reference.unit_a}/patients/"
        )
        _search(desk, "Paciente Sintetico 071601")
        expect(
            desk.locator("tbody tr", has_text="Paciente Sintetico 071601")
        ).to_have_count(1)
        capture(desk, renewal_artifact_root, "acceptance", "s16-imported")
    finally:
        operator.close()
        reception.close()
    flagship.blocked(reference, "competitor_import_formats", "unit_a")
    flagship.adapter(
        "import", "real", "apps.interop dry run, signed cutover, bulk batch commit"
    )
    flagship.cover(
        "S16.partner-migration",
        "failure",
        rows=7,
        accepted=4,
        rejected=3,
        duplicates_routed=True,
        reconciliation=reconciliation,
    )


def test_s17_missing_tenant_key_fails_closed_not_from_rows(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    """Rows without their key material never read as a successful recovery.

    The full restore drill (DB + objects + keys) is todo 72; here the served
    runtime has every SQL row while the tenant KEK is absent, as after a restore
    that lost the key escrow, and the product must refuse rather than render.
    """
    reference = reference_clinic
    secret_dir = Path(os.environ["CLINIC_SECRET_DIR"])
    kek = secret_dir / KEK_FILE
    escrow = secret_dir / f"{KEK_FILE}.escrow"
    # The protected name carries a sentinel: the failure path must not leak it.
    name = f"Paciente {new_sentinel('RESTORE-NAME')}"
    seed_patient(
        {
            "dsn": reference.dsn,
            "organization": reference.organization,
            "clinic_a": reference.unit_a,
            "receptionist_id": reference.personas["reception_a2"].user_id,
        },
        name,
    )
    flagship.sentinel("RESTORE-NAME")
    browser = _browser(renewal_page)
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in(page, renewal_base_url, reference, "reception_a2")
        patients = f"{renewal_base_url}/intake/clinics/{reference.unit_a}/patients/"
        goto_settled(page, patients)
        _search(page, name)
        expect(page.locator("tbody tr", has_text=name)).to_have_count(1)
        kek.rename(escrow)
        try:
            page.locator("#id_q").fill(name)
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST" and "/patients/" in response.url
                )
            ) as refused:
                page.locator(SEARCH).click()
            wait_for_js(page, SETTLED_JS)
            assert refused.value.status >= 500 or refused.value.status == 403
            # The refusal renders no protected value from the rows alone.
            assert name not in refused.value.text()
            capture(page, renewal_artifact_root, "acceptance", "s17-key-missing")
            readiness = page.request.get(f"{renewal_base_url}/readyz").status
        finally:
            escrow.rename(kek)
        goto_settled(page, patients)
        _search(page, name)
        expect(page.locator("tbody tr", has_text=name)).to_have_count(1)
        capture(page, renewal_artifact_root, "acceptance", "s17-key-restored")
    finally:
        context.close()
    flagship.blocked(reference, "hosted_pitr", "unit_a")
    flagship.blocked(reference, "tenant_key_management", "unit_a")
    flagship.cover(
        "S17.restore-missing-keys",
        "failure",
        refused_status=refused.value.status,
        readyz_status=readiness,
        dr_drill="todo-72",
    )


def _org_operator(reference: ReferenceClinic) -> dict[str, str]:
    """An organization admin of every clinic the session's fixtures created."""
    operator = {
        "id": str(uuid4()),
        "username": f"ref-org-operator-{uuid4().hex[:5]}",
        "totp_key": new_totp_key(),
    }
    with reference.owner_connection() as connection:
        connection.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id, username, password, email, first_name, last_name, is_active, "
            "is_staff, is_superuser, date_joined) "
            "VALUES (%s, %s, %s, '', '', '', true, false, false, now())",
            [operator["id"], operator["username"], make_password(reference.password)],
        )
        clinics = connection.execute(
            "SELECT id FROM clinic_app.identity_clinic WHERE organization_id = %s",
            [reference.organization],
        ).fetchall()
        for (clinic,) in clinics:
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role, "
                "workflow_bundle_version) VALUES (%s, %s, %s, %s, 'org_admin', 1)",
                [str(uuid4()), operator["id"], reference.organization, clinic],
            )
        seed_totp_device(connection, operator["id"], operator["totp_key"])
    return operator


def _subscription_event(
    reference: ReferenceClinic, operator: str, verb: str, **options: str
) -> str:
    """Run the owner's ``subscription_event`` CLI (real collection: EG-18)."""
    argv = [sys.executable, "manage.py", "subscription_event", verb]
    for name, value in options.items():
        argv += [f"--{name.replace('_', '-')}", value]
    if verb == "apply":
        argv += [
            "--operator-id",
            operator,
            "--organization-id",
            reference.organization,
            "--clinic-id",
            reference.unit_a,
        ]
    completed = subprocess.run(  # noqa: S603 - fixed command, opaque ids
        argv,
        env={
            **os.environ,
            "APP_DATABASE_URL": reference.dsn,
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        cwd=REPOSITORY,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip()


def _commercial(page: Page, base: str, clinic: str) -> dict[str, object]:
    tokens = [c for c in page.context.cookies() if "csrf" in c["name"].lower()]
    assert len(tokens) == 1
    response = page.request.post(
        base + SUBSCRIPTIONS,
        data=json.dumps({"clinic_id": clinic, "action": "read"}),
        headers={
            "Content-Type": "application/json",
            "X-CSRFToken": str(tokens[0]["value"]),
            "Referer": page.url,
        },
    )
    assert response.status == OK, response.status
    body: dict[str, object] = response.json()
    return body


def _subscription(page: Page, base: str, clinic: str) -> dict[str, object]:
    state = _commercial(page, base, clinic)["subscription"]
    assert isinstance(state, dict)
    return state


def test_s18_failed_subscription_payment_restricts_without_losing_care(  # noqa: PLR0913, PLR0915 - one lifecycle
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    operator = _org_operator(reference)
    browser = _browser(renewal_page)
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in_manager(
            page,
            renewal_base_url,
            {"dsn": reference.dsn, "password": reference.password},
            operator,
        )
        before = _subscription(page, renewal_base_url, reference.unit_a)
        assert before["state"] == "trial"
        assert _subscription_event(reference, operator["id"], "bootstrap-synthetic")

        def apply(action: str) -> str:
            return _subscription_event(
                reference,
                operator["id"],
                "apply",
                subscription_id=str(before["subscription_id"]),
                event_id=str(uuid4()),
                action=action,
            )

        assert apply("paid") == "active"
        assert apply("payment_failed") == "past_due"
        current = _subscription(page, renewal_base_url, reference.unit_a)
        assert (current["state"], current["notice_code"]) == (
            "past_due",
            "payment_past_due",
        )
        assert apply("advance") == "grace"
        grace = _subscription(page, renewal_base_url, reference.unit_a)
        assert grace["state"] == "grace"
        assert grace["notice_code"] == "payment_grace_deadline"
        assert grace["grace_ends_at"]
        # The grace deadline passes: the database clock moves, as in the
        # subscription tests; the owner restores it right after.
        with reference.owner_connection() as connection:
            row = connection.execute(
                "SELECT pg_get_functiondef("
                "'clinic_app.subscription_clock()'::regprocedure)"
            ).fetchone()
            assert row is not None
            original = str(row[0])
            # The clock is owned by clinic_resolver; the fixture owner holds SET.
            connection.execute("SET ROLE clinic_resolver")
            connection.execute(
                "CREATE OR REPLACE FUNCTION clinic_app.subscription_clock() "
                "RETURNS timestamptz LANGUAGE sql VOLATILE "
                "SET search_path=pg_catalog,clinic_app,pg_temp AS "
                "$f$ SELECT statement_timestamp() + interval '8 days' $f$"
            )
        try:
            assert apply("advance") == "restricted"
        finally:
            with reference.owner_connection() as connection:
                connection.execute("SET ROLE clinic_resolver")
                connection.execute(original)
        restricted = _commercial(page, renewal_base_url, reference.unit_a)
        allowances = restricted["allowances"]
        assert isinstance(allowances, dict)
        for meter in ("ai_tokens_in", "ai_audio_seconds"):
            entry = allowances[meter]
            assert isinstance(entry, dict)
            assert entry["available"] is False
        capture(page, renewal_artifact_root, "acceptance", "s18-restricted")
    finally:
        context.close()
    # Clinical care continues: a clinician still saves a manual note.
    # A previous scenario can leave the receptionist's conversation polling.
    # Its responses must not share cookies with the physician's fresh login.
    note_context = staff_context(browser, 1280)
    try:
        note_page = note_context.new_page()
        open_capture(note_page, renewal_base_url, availability_staff, "2047-04-08")
        with note_page.expect_response(
            lambda response: (
                "autosave" in response.url and response.request.method == "POST"
            ),
        ) as saved:
            note_page.locator("#id_subjective").fill("Atendimento sintético mantido.")
        assert saved.value.status == OK
    finally:
        note_context.close()
    flagship.blocked(reference, "saas_collection", "unit_a")
    flagship.cover(
        "S18.subscription-payment-failed",
        "failure",
        states=["trial", "active", "past_due", "grace", "restricted"],
        manual_note_saved=True,
    )


def test_s19_guardian_reads_are_logged_and_scoped(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    staff, base = availability_staff, renewal_base_url
    browser = _browser(renewal_page)
    represented = seed_encounter(staff, S19_DAY)
    manager = seed_manager(staff)
    guardian = portal._seed(
        staff,
        name="Responsável Sintético S19",
        operations=("enrollment_view", "records"),
    )
    contexts = [portal._context(browser, 1280) for _ in range(3)]
    physician, delegate, patient = (context.new_page() for context in contexts)
    try:
        # The represented patient's record is finalized and released for real.
        version = retention.open_finalized(
            physician, staff, base, S19_DAY, represented["specialty"]
        )
        # The screen releases delegate-restricted only, so the release the
        # guardian may read runs the release service for this exact version.
        released = run_fixture(
            staff["dsn"],
            "release-for-delegates",
            {
                "actor": staff["physician_a_id"],
                "clinic": staff["clinic_a"],
                "organization": staff["organization"],
                "version": version,
            },
        )
        assert released == {"released": True}
        scope = {
            "actor": manager["id"],
            "clinic": staff["clinic_a"],
            "organization": staff["organization"],
        }
        granted = run_fixture(
            staff["dsn"],
            "delegate-grant",
            {
                **scope,
                "represented": represented["enrollment"],
                "guardian": guardian["enrollment"],
                "evidence": seed_evidence(staff, represented),
            },
        )
        assert granted["state"] == "active"
        grant = str(granted["grant"])
        # The guardian switches to the representation and reads the record.
        _redeem(delegate, base, staff["clinic_a"], guardian["code"])
        delegate.locator("#portal-switch-grant").select_option(grant)
        click_to_navigate(delegate.locator("[data-portal-switch] button[type=submit]"))
        goto_settled(delegate, f"{base}/patient/records/")
        expect(delegate.get_by_text(retention.FIRST["subjective"])).to_be_visible()
        capture(delegate, renewal_artifact_root, "acceptance", "s19-delegate-read")
        # The read is in the represented patient's own access log.
        _redeem(
            patient,
            base,
            staff["clinic_a"],
            retention.grant_records(staff, represented),
        )
        reads = patient.locator(
            f'li[data-access-kind="delegate"][data-delegate-grant="{grant}"]'
        )
        goto_settled(patient, base + portal.ACCESS_LOG_PATH)
        expect(reads).to_have_count(1)
        expect(reads).to_have_attribute("data-record-type", "clinical_record")
        # The guardian authority changes: the clinic revokes the grant.
        revoked = run_fixture(
            staff["dsn"],
            "delegate-revoke",
            {**scope, "grant": grant, "revision": str(granted["revision"])},
        )
        assert revoked["state"] == "revoked"
        # The same open session's next request is re-evaluated.
        goto_settled(delegate, f"{base}/patient/records/")
        expect(delegate.get_by_text(retention.FIRST["subjective"])).to_have_count(0)
        capture(delegate, renewal_artifact_root, "acceptance", "s19-after-revoke")
        reload_settled(patient)
        expect(reads).to_have_count(1)
    finally:
        for context in contexts:
            context.close()
    flagship.cover(
        "S19.guardian-access-changes",
        "adversarial",
        delegated_reads_before=1,
        grant_revoked=True,
        content_after_revoke=0,
        delegated_reads_after=1,
    )


def test_s20_urgent_concern_reaches_the_accountable_queue(  # noqa: PLR0913 - scenario fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S20.urgent-concern",
        "happy",
        messages.test_an_urgent_portal_reply_reaches_the_accountable_urgent_queue,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )
    flagship.blocked(reference_clinic, "whatsapp", "unit_a")
