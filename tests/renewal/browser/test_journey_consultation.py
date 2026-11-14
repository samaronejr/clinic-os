"""J2: a consultation that produces completed work (todo 75 ``journey-consultation``).

booking -> confirmation and pre-visit forms -> arrival -> clinician brief ->
capture or manual documentation -> draft review -> finalization ->
prescription/document release -> payment/claim -> follow-up.

The spine is the continuous synthetic clinic day of ``test_end_to_end``: one
patient, one clinic, one encounter, one author and one document version
carried by reception, the physician, the patient and a public verifier
through booking, verified contact, questionnaire, arrival, notes, video,
finalization, a signed and released document, PIX instructions and receipt,
and the record export; the desktop and mobile days run it whole, the recovery
day interrupts every step after the server committed. The legs that the
spine's synthetic clinic does not exercise run through their owning todo's
proofs in the same runtime: the clinician Today brief, consented capture and
AI draft review with step-up finalization, prescribing, recorded PSP
payment, and clinician-approved follow-up tasks. Insurance claims have no
browser surface; the TISS submission leg stays BLOCKED-ON-EG (EG-8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import renewal.browser.test_billing_psp as psp
import renewal.browser.test_care_coordination as care
import renewal.browser.test_clinical_inbox as inbox
import renewal.browser.test_end_to_end as day
import renewal.browser.test_prescribing as prescribing
import renewal.browser.test_scribe_capture as scribe
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_care_coordination import care_staff
from renewal.browser.test_scribe_capture import capture_page

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = ("capture_page", "care_staff")
SPINE = {run[0]: run for run in day.RUNS}


def _day(  # noqa: PLR0913 - the spine run needs its full context
    flagship: FlagshipReport,
    branch: str,
    run: str,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    video: Path | None = None,
) -> None:
    name, width, civil_day, recovery = SPINE[run]
    rerun(
        flagship,
        f"J2.spine-{run}",
        branch,
        day.clinic_day,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        name=name,
        width=width,
        day=civil_day,
        recovery=recovery,
        video=video,
    )


def test_j2_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    for name, adapter in day.ADAPTERS.items():
        flagship.adapter(
            name,
            "real" if adapter["label"] == "real" else "synthetic",
            str(adapter["detail"]),
        )
    flagship.adapter("asr_llm", "synthetic", "recorded ASR + deterministic draft model")
    flagship.adapter("psp", "synthetic", "recorded-psp-v1 fixture provider")
    for capability in (
        "video",
        "qualified_signing",
        "physician_registration",
        "email",
        "pix",
        "psp_card",
        "asr",
        "llm_inference",
        "tiss",
    ):
        flagship.blocked(reference_clinic, capability, "unit_a")
    flagship.cover("J2.claim", "blocked", tiss="BLOCKED-ON-EG-8")


def test_j2_desktop_day_from_booking_to_receipt(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    # The QA film of the whole day: E/j2/<actor>.webm (reception, physician,
    # patient), recorded by the real contexts of the spine run.
    films = renewal_artifact_root / "E" / "j2"
    films.mkdir(mode=0o700, parents=True, exist_ok=True)
    _day(
        flagship,
        "happy",
        "desktop",
        renewal_base_url,
        renewal_artifact_root,
        availability_staff,
        video=films,
    )
    recorded = sorted(path.name for path in films.glob("*.webm"))
    assert recorded == ["patient.webm", "physician.webm", "reception.webm"], recorded
    flagship.cover("J2.film", "happy", width=1280, films=len(recorded))
    for step in (
        "booking",
        "confirmation",
        "forms",
        "arrival",
        "documentation",
        "finalize",
        "document-release",
        "payment",
        "export",
    ):
        flagship.cover(f"J2.{step}", "happy", width=1280, spine=True)


def test_j2_mobile_day_from_booking_to_receipt(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    _day(
        flagship,
        "happy",
        "mobile",
        renewal_base_url,
        renewal_artifact_root,
        availability_staff,
    )


def test_j2_recovery_day_never_claims_false_success(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    _day(
        flagship,
        "failure",
        "recovery",
        renewal_base_url,
        renewal_artifact_root,
        availability_staff,
    )


def test_j2_clinician_brief_on_today(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.brief",
        "happy",
        inbox.test_today_shows_next_patient_brief_and_inbox_counts,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j2_authorized_capture(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.capture",
        "happy",
        scribe.test_capture_upload_pause_resume_and_stop,
        capture_page=capture_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j2_draft_review_and_step_up_finalization(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.review",
        "happy",
        scribe.test_encounter_ai_review,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        journey="happy",
    )


def test_j2_prescription_signed_and_released(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.prescription",
        "happy",
        prescribing.test_prescribing_journey,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_j2_card_payment_pays_only_on_a_sealed_receipt(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.payment",
        "adversarial",
        psp.test_recorded_psp_instructions_never_pay_until_a_sealed_receipt,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        case=("card", 375, False),
    )


def test_j2_approved_follow_up_tasks(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.follow-up",
        "happy",
        care.test_clinician_request_and_staff_bundle_approval,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        care_staff=care_staff,
    )


def test_j2_invalid_follow_up_item_is_refused(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J2.follow-up",
        "failure",
        care.test_one_invalid_item_shows_an_item_level_refusal,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        care_staff=care_staff,
    )
