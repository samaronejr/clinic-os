"""Acceptance scenarios sec.12 rows 1-10 (todo 75 ``acceptance-scenarios``).

Each row runs end to end in the flagship runtime (every synthetic adapter,
realtime stack, worker DSN and the runner's PHI-sentinel scan of all sinks).
Where the owning todo already proved the row through the real pages, the row
re-runs that proof unchanged here (``rerun``): the same screens, forms and
assertions, now next to the D-17 reference clinic and every other adapter.
Rows the owning todos proved only below the browser (5, 8, 9) get new browser
journeys that drive the real screens and read the stored outcome back as the
runtime role. AI and ASR are deterministic synthetic fakes; their live legs
stay BLOCKED-ON-EG.
"""

# Injects SINTETICO-SENTINELA-* values into protected fields (runner scan).
from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from django.utils.translation import gettext
from ops.testing.sentinel_scan import new_sentinel
from playwright.sync_api import expect

import renewal.browser.results_extraction as extraction
import renewal.browser.test_agenda as agenda
import renewal.browser.test_agenda_grid as grid
import renewal.browser.test_scribe_capture as scribe
from renewal.browser._navigation import click_to_navigate, goto_settled, reload_settled
from renewal.browser.flagship_support import capture, run_fixture
from renewal.browser.scribe_review_journeys import worker
from renewal.browser.support_managers import seed_ai_admin
from renewal.browser.test_agenda import agenda_staff
from renewal.browser.test_agenda_grid import (
    board,
    grid_report,
    grid_root,
    grid_state,
    grid_world,
)
from renewal.browser.test_clinic_settings import seed_proposal
from renewal.browser.test_retention import sign_in_manager
from renewal.browser.test_scribe_capture import capture_page, open_capture, outage_page

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest
    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = (
    "agenda_staff",
    "board",
    "capture_page",
    "grid_report",
    "grid_root",
    "grid_state",
    "grid_world",
    "outage_page",
)
UNCERTAIN = (
    "Uncertain or inaudible span. Verify against the source; do not infer a dose."
)


def rerun(
    flagship: FlagshipReport,
    row: str,
    branch: str,
    source: Callable[..., None],
    **arguments: object,
) -> None:
    """Re-run the owning todo's browser proof of ``row`` and record it."""
    source(**arguments)
    module = source.__module__.rsplit(".", 1)[-1]
    flagship.cover(row, branch, proof=f"{module}::{source.__name__}", rerun=True)


def _runtime_count(staff: dict[str, str], actor: str, sql: str, *args: str) -> int:
    """Count rows as the runtime role inside the actor's tenant scope."""
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true),"
            " set_config('app.current_user_id', %s, true)",
            [staff["organization"], actor],
        )
        row = connection.execute(sql, list(args)).fetchone()
    assert row is not None
    return int(row[0])


def test_reference_clinic_ai_legs_stay_blocked(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter(
        "runtime", "real", "Gunicorn, Django middleware/CSRF/TOTP/RLS as clinic_app"
    )
    flagship.adapter(
        "asr", "synthetic", "RecordedASRAdapter fixtures (no recognition, no network)"
    )
    flagship.adapter(
        "llm", "synthetic", "ScribeSyntheticProvider / deterministic fake model"
    )
    flagship.adapter(
        "agent_executor", "real", "apps.ai approvals + execution as clinic_app"
    )
    flagship.adapter("realtime", "real", "task-owned Redis + SSE stream + proxy")
    for capability in ("asr", "llm_inference", "object_storage_media"):
        for unit in ("unit_a", "unit_b"):
            flagship.blocked(reference_clinic, capability, unit)
    assert len(reference_clinic.clinicians("unit_a")) == 6
    assert len(reference_clinic.rooms["unit_b"]) == 2


def test_s01_last_slot_race_holds_one_booking(
    board: object, flagship: FlagshipReport
) -> None:
    rerun(
        flagship,
        "S01.last-slot",
        "adversarial",
        grid.test_two_receptionists_racing_the_last_room_slot_hold_it_once,
        board=board,
    )


def test_s01_target_taken_meanwhile_keeps_the_original(
    board: object, flagship: FlagshipReport
) -> None:
    rerun(
        flagship,
        "S01.last-slot",
        "failure",
        grid.test_a_target_taken_meanwhile_leaves_the_original_unchanged,
        board=board,
    )


def test_s02_patient_switch_needs_an_explicit_audio_decision(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S02.patient-switch",
        "adversarial",
        scribe.test_patient_switch_requires_an_explicit_bound_audio_decision,
        capture_page=capture_page,
        renewal_base_url=renewal_base_url,
        availability_staff=availability_staff,
    )


def test_s03_recording_refused_keeps_manual_care(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S03.recording-declined",
        "happy",
        scribe.test_recording_refusal_hides_capture_without_blocking_care,
        capture_page=capture_page,
        renewal_base_url=renewal_base_url,
        availability_staff=availability_staff,
    )


def test_s03_declined_microphone_keeps_manual_save(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S03.recording-declined",
        "failure",
        scribe.test_declined_microphone_keeps_manual_clinical_save,
        capture_page=capture_page,
        renewal_base_url=renewal_base_url,
        availability_staff=availability_staff,
    )


def test_s04_outage_pauses_and_records_gaps(
    outage_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S04.network-mic-failure",
        "failure",
        scribe.test_seventy_second_outage_pauses_drops_expired_audio_and_records_gaps,
        outage_page=outage_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_s04_pending_transcript_is_never_fabricated(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S04.network-mic-failure",
        "adversarial",
        scribe.test_transcript_pending_incomplete_final_and_manual_care,
        capture_page=capture_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_s05_ambiguous_dose_is_flagged_and_never_prescribed(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    staff = availability_staff
    page = capture_page
    data = open_capture(page, renewal_base_url, staff, "2047-03-04")
    seeded = run_fixture(
        staff["dsn"],
        "scribe-ambiguous",
        {
            "actor": staff["physician_a_id"],
            "clinic": staff["clinic_a"],
            "organization": staff["organization"],
            "appointment": data["appointment"],
        },
    )
    # The synthetic ASR heard "5 ou 15 mg" below the confidence floor.
    assert seeded["uncertain_segments"] == 1
    version = str(seeded["version"])
    reload_settled(page)
    click_to_navigate(page.locator('form[action$="/scribe/review/"] button'))
    click_to_navigate(page.locator('button[value="generate"]'))
    assert worker(staff, "execute", version)["inference"] == "succeeded"
    click_to_navigate(page.locator('button[value="refresh"]'))
    # The draft cites the uncertain span as uncertain; nothing infers a dose.
    expect(
        page.locator("p.feedback", has_text=gettext(UNCERTAIN)).first
    ).to_be_visible()
    capture(page, renewal_artifact_root, "acceptance", "s05-review-uncertain")
    assert worker(staff, "inspect", version)["state"] == "draft"
    click_to_navigate(page.locator('a[href$="/encounter/"]').first)
    click_to_navigate(page.locator('form[action$="/scribe/transcript/"] button'))
    expect(page.locator("p.feedback", has_text=gettext(UNCERTAIN))).to_have_count(1)
    capture(page, renewal_artifact_root, "acceptance", "s05-transcript-uncertain")
    # Failure branch: no prescription was drafted from the ambiguous dose.
    drafts = _runtime_count(
        staff,
        staff["physician_a_id"],
        "SELECT count(*) FROM clinic_app.prescription_prescriptiondraft d "
        "JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id "
        "WHERE e.appointment_id = %s",
        data["appointment"],
    )
    assert drafts == 0
    flagship.cover("S05.ambiguous-dose", "adversarial", uncertain_segments=1, drafts=0)


def test_s06_late_model_result_preserves_edits(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S06.late-model-result",
        "failure",
        scribe.test_encounter_ai_review,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        journey="late",
    )


def test_s07_document_instructions_are_data(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S07.malicious-document",
        "adversarial",
        extraction.test_document_extraction_five_values_unreadable_and_injection,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        javascript=True,
    )


def test_s07_foreign_model_citation_never_reaches_the_note(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S07.malicious-document",
        "failure",
        scribe.test_encounter_ai_review,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        journey="foreign",
    )


def _agent_queue(
    renewal_page: Page, base: str, staff: dict[str, str], theme: str
) -> tuple[Page, dict[str, str], str]:
    manager = seed_ai_admin(staff, theme)
    # Organization policy needs configuration authority on every clinic the
    # session's fixtures created (reference units, agenda clinics).
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_userclinicrole (id, user_id, "
            "organization_id, clinic_id, role, workflow_bundle_version) "
            "SELECT gen_random_uuid(), %s, c.organization_id, c.id, 'org_admin', 1 "
            "FROM clinic_app.identity_clinic c WHERE c.organization_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r "
            "WHERE r.user_id = %s AND r.clinic_id = c.id AND r.role = 'org_admin')",
            [manager["id"], staff["organization"], manager["id"]],
        )
    proposal, _revision = seed_proposal(staff, manager)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
    )
    page = context.new_page()
    sign_in_manager(page, base, staff, manager)
    goto_settled(page, f"{base}/clinics/{staff['clinic_a']}/automations/agents/")
    row = page.locator(f'[data-agent-proposal="{proposal}"]')
    expect(row).to_have_attribute("data-agent-state", "validated")
    return page, manager, proposal


def _effects(staff: dict[str, str], manager: str, proposal: str) -> tuple[int, int]:
    approvals = _runtime_count(
        staff,
        manager,
        "SELECT count(*) FROM clinic_app.ai_approval WHERE proposal_id = %s",
        proposal,
    )
    receipts = _runtime_count(
        staff,
        manager,
        "SELECT count(*) FROM clinic_app.ai_actionreceipt r "
        "JOIN clinic_app.ai_actionexecution e ON e.id = r.execution_id "
        "WHERE e.proposal_id = %s",
        proposal,
    )
    return approvals, receipts


def test_s08_proposal_from_a_superseded_source_is_refused(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    staff = availability_staff
    page, manager, proposal = _agent_queue(
        renewal_page, renewal_base_url, staff, "light"
    )
    try:
        # The plan the proposal cites is replaced after the proposal was shown.
        superseded = run_fixture(
            staff["dsn"],
            "agent-supersede",
            {
                "actor": manager["id"],
                "clinic": staff["clinic_a"],
                "organization": staff["organization"],
                "proposal": proposal,
            },
        )
        assert superseded["superseded_revision"]
        row = page.locator(f'[data-agent-proposal="{proposal}"]')
        click_to_navigate(row.locator('button[value="approve"]'))
        expect(page.locator('[role="alert"]').first).to_be_visible()
        capture(page, renewal_artifact_root, "acceptance", "s08-superseded")
        stale = page.locator(f'[data-agent-proposal="{proposal}"]')
        if stale.count():
            expect(stale).not_to_have_attribute("data-agent-state", "receipt")
        assert _effects(staff, manager["id"], proposal) == (0, 0)
    finally:
        page.context.close()
    flagship.cover("S08.superseded-plan", "adversarial", approvals=0, receipts=0)


def test_s09_authority_revoked_after_review_blocks_execution(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    staff = availability_staff
    page, manager, proposal = _agent_queue(
        renewal_page, renewal_base_url, staff, "dark"
    )
    try:
        # The approver's clinic authority is revoked while the queue is open;
        # the same revocation fact the offboarding screen records.
        with psycopg.connect(staff["dsn"]) as connection:
            connection.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [staff["organization"]],
            )
            revoked = connection.execute(
                "UPDATE clinic_app.identity_userclinicrole SET revoked_at = now() "
                "WHERE user_id = %s AND clinic_id = %s AND revoked_at IS NULL",
                [manager["id"], staff["clinic_a"]],
            ).rowcount
        assert revoked >= 1
        row = page.locator(f'[data-agent-proposal="{proposal}"]')
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and "/automations/agents/" in response.url
            )
        ) as answered:
            click_to_navigate(row.locator('button[value="approve"]'))
        # Refused: a denial or a redirect away from the queue, never a receipt.
        assert answered.value.status in {302, 303, 403, 404, 409}
        capture(page, renewal_artifact_root, "acceptance", "s09-revoked")
        assert _effects(staff, manager["id"], proposal) == (0, 0)
    finally:
        page.context.close()
    # Adversarial by construction: authority revoked between review and use.
    flagship.cover(
        "S09.authority-revoked", "adversarial", status_refused=True, receipts=0
    )


def test_s10_unauthorized_summary_leaks_nothing(  # noqa: PLR0913 - owner proof fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "S10.unauthorized-summary",
        "adversarial",
        agenda.test_today_source_cited_brief_refuses_other_patient_and_preserves_manual_chart,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        monkeypatch=monkeypatch,
    )


def test_reference_clinic_note_sentinel_stays_out_of_every_sink(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    """A clinician's note text carries a sentinel through the real SOAP save."""
    staff = availability_staff
    page = renewal_page.context.new_page()
    try:
        open_capture(page, renewal_base_url, staff, "2047-03-11")
        sentinel = new_sentinel("ACCEPTANCE-NOTE")
        note = f"Relato sintético {sentinel}"
        with page.expect_response(
            lambda response: (
                "autosave" in response.url and response.request.method == "POST"
            ),
        ) as saved:
            page.locator("#id_subjective").fill(note)
        assert saved.value.status == 200
        reload_settled(page)
        expect(page.locator("#id_subjective")).to_have_value(note)
        flagship.sentinel("ACCEPTANCE-NOTE")
    finally:
        page.close()
    flagship.cover("S00.sentinel-note", "adversarial", sinks="runner-scan")
