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
from django.conf import settings
from django.utils.translation import gettext
from ops.testing.sentinel_scan import new_sentinel
from playwright.sync_api import expect

import renewal.browser.results_extraction as extraction
import renewal.browser.test_agenda as agenda
import renewal.browser.test_agenda_grid as grid
import renewal.browser.test_scribe_capture as scribe
from renewal.browser._fixture_secrets import FixtureSecret
from renewal.browser._navigation import click_to_navigate, goto_settled, reload_settled
from renewal.browser._s09_support import (
    grant_org_admin,
    revoke_clinic_roles,
    seed_quorum_proposal,
)
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
    from playwright.sync_api import Locator, Page

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
    base = renewal_base_url
    browser = renewal_page.context.browser
    assert browser is not None
    first, second = seed_ai_admin(staff, "dark"), seed_ai_admin(staff, "light")
    for approver in (first, second):
        grant_org_admin(staff, approver["id"])
    queue = f"{base}/clinics/{staff['clinic_a']}/automations/agents/"
    # Seed before signing in: the first seeding rebinds the approvers' role
    # bundles, which ends any session they already hold. Each seeding also
    # publishes a newer policy, so the attack proposal is seeded only once the
    # control has executed under its own current policy.
    control = seed_quorum_proposal(staff, first["id"], second["id"])
    pages: list[Page] = []
    try:
        for approver in (first, second):
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="pt-BR",
                timezone_id="Asia/Tokyo",
            )
            pages.append(context.new_page())
            sign_in_manager(pages[-1], base, staff, approver)
        # Control: with both approvers' authority intact, the second vote is
        # the one that executes, so this setup can produce a receipt.
        _vote(pages[0], queue, control)
        # A refused vote names its reason on the page; keep it in the failure.
        assert _s09_state(staff, second["id"], control) == (1, 0, 0), (
            pages[0].locator('[role="alert"]').all_inner_texts()
        )
        _vote(pages[1], queue, control)
        expect(
            pages[1].locator(f'[data-agent-proposal="{control}"]')
        ).to_have_attribute("data-agent-state", "receipt")
        assert _s09_state(staff, second["id"], control) == (2, 1, 1)
        # Retain both approvals before changing authority. The second approval
        # uses the real service and this browser's persisted verification;
        # dispatch is a separate call so no executor is patched or skipped.
        proposal = seed_quorum_proposal(staff, first["id"], second["id"])
        _vote(pages[0], queue, proposal)
        assert _s09_state(staff, second["id"], proposal) == (1, 0, 0)
        goto_settled(pages[1], queue)
        reviewed = pages[1].locator(f'[data-agent-proposal="{proposal}"]')
        expect(reviewed).to_be_visible()
        cookies = {
            cookie["name"]: FixtureSecret(cookie["value"])
            for cookie in pages[1].context.cookies()
        }
        scope = {
            "actor": second["id"],
            "organization": staff["organization"],
            "clinic": staff["clinic_a"],
            "proposal": proposal,
        }
        approved = run_fixture(
            staff["dsn"],
            "approve-without-dispatch",
            {
                **scope,
                "session": cookies[settings.SESSION_COOKIE_NAME],
                "digest": reviewed.locator('input[name="digest"]').first.input_value(),
            },
        )
        assert approved == {"state": "approved"}
        assert _s09_state(staff, second["id"], proposal) == (2, 0, 0)
        assert revoke_clinic_roles(staff, first["id"]) >= 1
        executed = run_fixture(staff["dsn"], "execute-approved", scope)
        assert executed == {
            "state": "rejected",
            "reason": "authority_revoked",
            "execution_created": False,
            "receipt_created": False,
        }
        goto_settled(pages[1], queue)
        row = pages[1].locator(f'[data-agent-proposal="{proposal}"]')
        expect(row).to_have_attribute("data-agent-state", "rejected")
        capture(pages[1], renewal_artifact_root, "acceptance", "s09-revoked")
        # Execution re-evaluated the stale approval: no execution, no receipt.
        assert _s09_state(staff, second["id"], proposal) == (2, 0, 0)
        assert _proposal_state(staff, second["id"], proposal) == "rejected"
    finally:
        for page in pages:
            page.context.close()
    # Adversarial by construction: authority revoked between approval and use.
    flagship.cover(
        "S09.authority-revoked",
        "adversarial",
        control_receipts=1,
        approvals_before_revocation=2,
        executor_called=True,
        executions=0,
        receipts=0,
    )


def _vote(page: Page, queue: str, proposal: str) -> Locator:
    """Approve ``proposal``'s exact effect from the reviewer's own session."""
    goto_settled(page, queue)
    row = page.locator(f'[data-agent-proposal="{proposal}"]')
    expect(row).to_be_visible()
    click_to_navigate(row.locator('button[value="approve"]'))
    return page.locator(f'[data-agent-proposal="{proposal}"]')


def _s09_state(staff: dict[str, str], reader: str, proposal: str) -> tuple[int, ...]:
    """Approvals, executions and receipts of ``proposal``, read as clinic_app."""
    return (
        _runtime_count(
            staff,
            reader,
            "SELECT count(*) FROM clinic_app.ai_approval WHERE proposal_id = %s",
            proposal,
        ),
        _runtime_count(
            staff,
            reader,
            "SELECT count(*) FROM clinic_app.ai_actionexecution WHERE proposal_id = %s",
            proposal,
        ),
        _effects(staff, reader, proposal)[1],
    )


def _proposal_state(staff: dict[str, str], reader: str, proposal: str) -> str:
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true),"
            " set_config('app.current_user_id', %s, true)",
            [staff["organization"], reader],
        )
        row = connection.execute(
            "SELECT state FROM clinic_app.ai_actionproposal WHERE id = %s", [proposal]
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_s10_unauthorized_summary_leaks_nothing(  # noqa: PLR0913 - owner proof fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    flagship: FlagshipReport,
) -> None:
    agenda.test_today_source_cited_brief_refuses_other_patient_and_preserves_manual_chart(
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        monkeypatch=monkeypatch,
    )
    flagship.cover(
        "S10.unauthorized-summary",
        "adversarial",
        principal="authenticated_receptionist",
        commands=["generate", "display"],
        denial_status=403,
        same_as_unknown=True,
        source_and_generated_content_absent=True,
        authorized_source_visible_before_and_after=True,
        forbidden_question_refusal_retained=True,
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
