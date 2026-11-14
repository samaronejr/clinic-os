"""J5: results to resolution (todo 75 ``journey-results``).

A result is received -> the patient/order match is verified -> extraction is
reviewed -> the clinician acknowledges and decides -> the approved
communication or follow-up is delivered -> unresolved items stay accountable
to an owner. The receipt leg is the signed synthetic lab callback (HMAC on
raw bytes before any scope lookup, replay files once) and the staff upload
whose wrong-patient document goes to quarantine; extraction is the
deterministic fake model with an injection-bearing document treated as data;
acknowledgment and release run from the Clinical Inbox (one queue, item-level
bulk previews, a revoked item removed with a notice); release reaches the
patient only after the clinician's approval; follow-up is a clinician-approved
care bundle. Each leg is the owning todo's proof (31, 32, 45, 52, 55) re-run
in the flagship runtime; live lab/RNDS connectivity stays BLOCKED-ON-EG.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import renewal.browser.results_extraction as extraction
import renewal.browser.test_care_coordination as care
import renewal.browser.test_clinical_inbox as inbox
import renewal.browser.test_results as results
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_care_coordination import care_staff

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = ("care_staff",)


def test_j5_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("lab", "synthetic", "ORDERS_SYNTHETIC_LAB_ENABLED signed callback")
    flagship.adapter("extraction", "synthetic", "deterministic fake extraction model")
    for capability in ("rnds", "llm_inference", "attachment_scanning", "email"):
        flagship.blocked(reference_clinic, capability, "unit_a")


def test_j5_signed_lab_result_files_once(
    renewal_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.received",
        "adversarial",
        results.test_signed_callback_http_replay_has_one_version_and_one_task,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        availability_staff=availability_staff,
    )


def test_j5_match_acknowledge_release_and_quarantine(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.match-verified",
        "failure",
        results.test_result_upload_acknowledgment_release_and_wrong_patient,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=375,
    )
    flagship.cover("J5.release", "happy", spine="results-upload")


def test_j5_results_errors_offline_and_scope_refusal(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.match-verified",
        "adversarial",
        results.test_results_errors_offline_keyboard_and_scope_refusal,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j5_extraction_review_treats_document_text_as_data(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.extraction-reviewed",
        "adversarial",
        extraction.test_document_extraction_five_values_unreadable_and_injection,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        javascript=False,
    )


def test_j5_clinician_acknowledges_from_the_inbox(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.acknowledged",
        "happy",
        inbox.test_inbox_acknowledges_a_result_from_the_queue,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_j5_bulk_acknowledgment_never_releases(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.acknowledged",
        "failure",
        inbox.test_bulk_acknowledgment_previews_each_normal_result_and_never_releases,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j5_unresolved_items_stay_accountable(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.accountable-owner",
        "failure",
        inbox.test_revoked_item_is_removed_with_a_notice_on_refresh,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j5_approved_follow_up_is_delivered(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.follow-up",
        "happy",
        care.test_clinician_request_and_staff_bundle_approval,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        care_staff=care_staff,
    )


def test_j5_fake_medication_change_is_rejected(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J5.follow-up",
        "adversarial",
        care.test_fake_medication_change_is_rejected_by_the_validator,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        care_staff=care_staff,
    )
