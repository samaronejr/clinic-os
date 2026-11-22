"""J5: results to resolution (todo 75 ``journey-results``).

The connected case carries one signed lab callback document through matching,
extraction review, the ordering physician's inbox, acknowledgment, explicit
release and an exact-byte patient download. Runtime reads assert the same
order and version throughout. Neither ingestion nor acknowledgment releases
the document; another clinic's physician cannot see the inbox item.

Supplementary proofs cover wrong-patient quarantine, callback replay,
injection-bearing extraction, bulk review, revoked access, care bundles and
their failure paths. Live lab/RNDS connectivity stays BLOCKED-ON-EG.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.utils.translation import gettext as _
from playwright.sync_api import expect

import renewal.browser.results_extraction as extraction
import renewal.browser.test_care_coordination as care
import renewal.browser.test_clinical_inbox as inbox
import renewal.browser.test_results as results
from ai.test_extraction import pdf_fixture
from renewal.browser._j5_chain import chain_identity
from renewal.browser._navigation import goto_settled
from renewal.browser.flagship_support import capture, sign_in
from renewal.browser.results_lab_support import runtime_effects, seed_delivery
from renewal.browser.results_support import (
    NOTICE,
    open_results_with_notice,
    seed_result_patient,
    submit,
)
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_availability import _sign_in_physician, _sign_in_receptionist
from renewal.browser.test_care_coordination import care_staff
from renewal.browser.test_patient_access import _issue_code, _open_access, _redeem

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = ("care_staff",)
OK: Final = 200
J5_DAY: Final = "2041-07-15"


def test_j5_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("lab", "synthetic", "ORDERS_SYNTHETIC_LAB_ENABLED signed callback")
    flagship.adapter("extraction", "synthetic", "deterministic fake extraction model")
    for capability in ("rnds", "llm_inference", "attachment_scanning", "email"):
        flagship.blocked(reference_clinic, capability, "unit_a")


def test_j5_one_result_from_receipt_to_resolution(  # noqa: PLR0913, PLR0915 - one chain
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    """One patient and order: receipt -> match -> extraction -> owner -> follow-up."""
    staff, base = availability_staff, renewal_base_url
    identity = seed_result_patient(staff, J5_DAY)
    # Receipt: the signed synthetic lab callback files exactly one version
    # and one accountable task for the ordering physician.
    document = pdf_fixture()
    delivery = seed_delivery(staff, identity, source=document)
    received = renewal_page.request.post(
        f"{base}/orders/lab/callback/", data=delivery.body, headers=delivery.headers
    )
    assert received.status == OK
    assert received.json()["state"] == "matched"
    assert runtime_effects(staff, delivery.event) == (1, 1)
    order, version, acknowledged = chain_identity(staff, identity["enrollment"])
    assert acknowledged == 0
    browser = renewal_page.context.browser
    assert browser is not None
    extraction.fixture(staff, "configure")
    owner = browser.new_context(locale="pt-BR", viewport={"width": 1280, "height": 900})
    other = browser.new_context(locale="pt-BR", viewport={"width": 1280, "height": 900})
    reception = browser.new_context(locale="pt-BR")
    portal = browser.new_context(locale="pt-BR", viewport={"width": 375, "height": 900})
    try:
        page = owner.new_page()
        _sign_in_physician(page, base, staff)
        receptionist = reception.new_page()
        _sign_in_receptionist(receptionist, base, staff)
        _open_access(receptionist, base, staff, identity["source_name"])
        patient = portal.new_page()
        _redeem(patient, base, staff["clinic_a"], _issue_code(receptionist))
        goto_settled(patient, f"{base}/patient/results/")
        expect(patient.locator("[data-patient-results]")).to_be_visible()
        expect(patient.locator("[data-patient-result]")).to_have_count(0)
        # Extract the exact source received by the callback, without creating
        # another order, uploading a replacement, or changing its version.
        goto_settled(page, f"{base}/clinics/{staff['clinic_a']}/results/")
        source = page.locator(
            f'form:has(input[name="result_version_id"][value="{version}"])'
        )
        extraction.post(
            page, source.get_by_role("button", name=_("Review extraction candidates"))
        )
        job = extraction.collect(page, staff)
        candidates = page.locator(".extraction-candidate")
        expect(candidates).to_have_count(5)
        extraction.confirm_value(page, candidates.first, unit="mg/dL")
        expect(
            page.get_by_text(_("Imported and confirmed"), exact=False)
        ).to_have_count(1)
        assert extraction.fixture(staff, "inspect", job_id=job)["acknowledged"] is False
        assert chain_identity(staff, identity["enrollment"]) == (order, version, 0)
        # The same result waits in the ordering physician's inbox.
        url = inbox.inbox_url(base, staff)
        keys = inbox.source_keys(staff, identity)
        assert len(keys) == 1
        goto_settled(page, url)
        items = inbox.results_of(page, keys)
        expect(items).to_have_count(1)
        expect(items.locator(".worklist-facts dd").first).to_have_text(_("You"))
        capture(page, renewal_artifact_root, "journey-results", "chain-inbox")
        items.locator('[name="disposition"]').select_option("follow_up")
        status = inbox.post(
            page, url, items.get_by_role("button", name=_("Acknowledge"))
        )
        assert status == OK
        expect(inbox.results_of(page, keys)).to_have_count(0)
        assert chain_identity(staff, identity["enrollment"]) == (order, version, 1)
        dispositions, releases = inbox.decisions(staff, identity)
        assert "follow_up" in dispositions
        assert releases == 0
        # Acknowledgment alone did not release anything. The clinician now
        # approves neutral wording and explicitly releases this exact version.
        open_results_with_notice(page, base, staff["clinic_a"])
        result = page.locator("article[data-result-state]").filter(
            has=page.locator(f'input[name="version_id"][value="{version}"]')
        )
        expect(result.locator("[data-release-notice]")).to_contain_text(NOTICE)
        submit(
            page,
            result.get_by_role("button", name=_("Release this version to patient")),
            "This version was released to the selected audience.",
        )
        goto_settled(patient, f"{base}/patient/results/")
        released = patient.locator("[data-patient-result]")
        expect(released).to_have_count(1)
        expect(released.locator('input[name="version_id"]')).to_have_value(version)
        with (
            patient.expect_download() as downloaded,
            patient.expect_response(
                lambda response: (
                    response.url == patient.url and response.request.method == "POST"
                )
            ) as download_response,
        ):
            released.locator("[data-result-download]").click()
        assert download_response.value.status == OK
        assert downloaded.value.path().read_bytes() == document
        assert chain_identity(staff, identity["enrollment"]) == (order, version, 1)
        assert inbox.decisions(staff, identity) == (["follow_up"], 1)
        capture(patient, renewal_artifact_root, "journey-results", "chain-delivered")
        # The other clinic's physician never sees this patient's results.
        foreign = other.new_page()
        sign_in(foreign, base, reference_clinic, "physician_b1")
        response = goto_settled(foreign, url)
        assert response is not None
        expect(inbox.results_of(foreign, keys)).to_have_count(0)
    finally:
        owner.close()
        other.close()
        reception.close()
        portal.close()
    flagship.cover(
        "J5.chain",
        "happy",
        received=1,
        matched=1,
        extraction_confirmed=1,
        acknowledged=1,
        follow_up=True,
        released=1,
        patient_downloaded=True,
    )
    flagship.cover("J5.accountable-owner", "adversarial", foreign_visible=0)


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
