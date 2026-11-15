"""J2's clinical and follow-up legs on the spine day's own encounter (todo 75).

``test_end_to_end.clinic_day`` carries one patient, clinic, encounter, author
and note version from booking to the record export. J2 passes these two steps
into that same day, so the pre-visit brief, the consented capture, the AI
draft review, the plan the clinician writes and the care follow-up all act on
the day's identities, and the day's films record them.

The capture leg replays synthetic audio through the real scoped scribe
services, as S05 does; it does not exercise the browser microphone controls.
Care-plan creation has no screen and uses ``create_care_plan_from_version``.
Both service legs execute as ``clinic_app``. Their subsequent review and
approval happen in the browser on this day's records.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

import renewal.browser.test_care_coordination as care
from renewal.browser._fixture_secrets import worker_dsn
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser.flagship_support import run_fixture
from renewal.browser.scribe_review_journeys import worker
from renewal.browser.support_managers import seed_ai_admin
from renewal.browser.test_clinical_inbox import _brief_command, _brief_post
from renewal.browser.test_encounter import press
from renewal.browser.test_scribe_capture import (
    fixture_command as scribe_fixture_command,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from renewal.browser.test_end_to_end import Day

UNCERTAIN: Final = (
    "Uncertain or inaudible span. Verify against the source; do not infer a dose."
)


def _enrollment(case: Day) -> str:
    with psycopg.connect(case.staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [case.staff["organization"]],
        )
        row = connection.execute(
            "SELECT id::text FROM clinic_app.intake_patientclinicenrollment "
            "WHERE patient_id = %s AND clinic_id = %s",
            [case.facts["patient_id"], case.clinic],
        ).fetchone()
    assert row is not None
    return str(row[0])


def _latest_operation(case: Day, enrollment: str) -> str:
    with psycopg.connect(case.staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [case.staff["organization"]],
        )
        row = connection.execute(
            "SELECT i.operation_id::text FROM clinic_app.ai_aiinvocation i "
            "JOIN clinic_app.ai_aiartifact a ON a.id = i.inputs_ref_id "
            "WHERE a.patient_enrollment_id = %s ORDER BY i.created_at DESC LIMIT 1",
            [enrollment],
        ).fetchone()
    assert row is not None
    return str(row[0])


def _encounter(case: Day, physician: Page) -> None:
    goto_settled(physician, case.url(f"/ehr/clinics/{case.clinic}/encounter/"))


def clinical(case: Day, pages: dict[str, Page]) -> None:
    """Brief, consent, capture, AI review and the plan on the open encounter."""
    staff = case.staff
    physician = pages["médico"]
    # Brief first: each AI step configures its own purpose, so the pre-visit
    # brief runs before the scribe steps configure capture and drafting.
    enrollment = _enrollment(case)
    # Chart reads require the current professional registration established by
    # this setup, as well as the recording consent used later in the visit.
    assert (
        scribe_fixture_command(
            staff,
            {"SCRIBE_FIXTURE_ENROLLMENT": enrollment, "SCRIBE_FIXTURE_ACCEPTED": "1"},
        )
        == b"SCRIBE_FIXTURE_READY"
    )
    administrator = seed_ai_admin(staff, "light")
    with pytest.MonkeyPatch.context() as patch:
        seeded = _brief_command(
            patch,
            staff["dsn"],
            "seed",
            administrator["id"],
            case.clinic,
            staff["organization"],
        )
        assert seeded["status"] == "seeded"
        _encounter(case, physician)
        _brief_post(
            physician,
            physician.get_by_role("button", name=gettext("Generate pre-visit brief")),
        )
        panel = physician.locator("#chart-panel")
        expect(panel).to_have_attribute("data-chart-state", "generating")
        executed = _brief_command(
            patch, worker_dsn(), "execute", _latest_operation(case, enrollment)
        )
        assert executed["status"] == "succeeded"
    _brief_post(
        physician, physician.get_by_role("button", name=gettext("Refresh brief"))
    )
    expect(panel).to_have_attribute("data-chart-state", "ready")
    # The physician informs the patient about AI assistance on this encounter.
    assert (
        scribe_fixture_command(
            staff,
            {
                "SCRIBE_FIXTURE_ACTION": "disclosure",
                "SCRIBE_FIXTURE_APPOINTMENT": str(case.facts["appointment_id"]),
                "SCRIBE_FIXTURE_AI_REFUSED": "0",
            },
        )
        == b"SCRIBE_FIXTURE_DISCLOSED"
    )
    # Capture: the consented recording of this appointment, transcribed by the
    # synthetic ASR into the same encounter's draft version.
    captured = run_fixture(
        staff["dsn"],
        "scribe-ambiguous",
        {
            "actor": staff["physician_a_id"],
            "clinic": case.clinic,
            "organization": staff["organization"],
            "appointment": str(case.facts["appointment_id"]),
        },
    )
    assert captured["version"] == case.facts["version_id"]
    # AI review: a source-linked draft of this version, the dose kept uncertain.
    _encounter(case, physician)
    click_to_navigate(physician.locator('form[action$="/scribe/review/"] button'))
    click_to_navigate(physician.locator('button[value="generate"]'))
    version = str(case.facts["version_id"])
    assert worker(staff, "execute", version)["inference"] == "succeeded"
    click_to_navigate(physician.locator('button[value="refresh"]'))
    expect(
        physician.locator("p.feedback", has_text=gettext(UNCERTAIN)).first
    ).to_be_visible()
    assert worker(staff, "inspect", version)["state"] == "draft"
    # The clinician writes the plan the follow-up will act on.
    _encounter(case, physician)
    physician.locator("#id_plan").fill(care.BUNDLE_PLAN)
    press(physician, "save")
    case.facts["chain"] = {
        "capture_version_matches": True,
        "brief": "ready",
        "review": "draft",
        "uncertain_segments": captured["uncertain_segments"],
    }


def follow_up(case: Day, pages: dict[str, Page]) -> None:
    """The finalized plan becomes a care bundle the staff approves."""
    staff = case.staff
    physician = pages["médico"]
    # The care purpose's organization policy, after the chain's scribe setup.
    assert care.fixture(staff, "configure") == {"synthetic": True}
    plan = run_fixture(
        staff["dsn"],
        "care-plan",
        {
            "actor": staff["physician_a_id"],
            "clinic": case.clinic,
            "organization": staff["organization"],
            "version": str(case.facts["version_id"]),
        },
    )["plan_id"]
    url = case.url(f"/clinics/{case.clinic}/automations/care-coordination/")
    goto_settled(physician, url)
    card = physician.locator(f'[data-care-plan="{plan}"]')
    click_to_navigate(card.get_by_role("button", name=gettext("Request suggestions")))
    expect(physician.locator("[data-care-generating]")).to_be_visible()
    invocation = physician.locator(
        'form:has(input[name="action"][value="refresh"]) input[name="invocation_id"]'
    ).input_value()
    assert care.fixture(staff, "dispatch", invocation=invocation) == {
        "runtime_role": "clinic_app",
        "state": "succeeded",
    }
    click_to_navigate(
        physician.get_by_role("button", name=gettext("Refresh suggestions"))
    )
    expect(physician.locator('[data-care-bundle="ready"]')).to_be_visible()
    browser = physician.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": case.width, "height": 900}
    )
    try:
        approver = context.new_page()
        care._manager(approver, case.base, staff)
        care._approve(approver, case.base, staff, invocation)
    finally:
        context.close()
    case.facts["chain_follow_up"] = {
        "care_plan_from_day_version": True,
        "bundle": "approved",
    }
