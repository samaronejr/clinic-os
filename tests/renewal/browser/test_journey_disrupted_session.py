"""J4: a disrupted clinical session (todo 75 ``journey-disrupted-session``).

Microphone denial, temporary disconnection, a stale note revision, an
interrupted video, delayed signing and a duplicate payment callback never
create false success: each screen shows the exact recoverable state and never
invents missing content. Every disruption is driven through the real pages
in the flagship runtime and its stored state is read back by the owning
todo's proof (27, 37, 40, 33-35, 57), re-run here in one session; the
duplicate PSP callback leg is the scenario-13 journey of
``test_acceptance_operations`` (forged, tampered, replayed and out-of-order
deliveries, one settlement).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import renewal.browser.test_acceptance_operations as operations
import renewal.browser.test_amendments as amendments
import renewal.browser.test_billing as billing
import renewal.browser.test_encounter as encounter
import renewal.browser.test_prescribing as prescribing
import renewal.browser.test_scribe_capture as scribe
import renewal.browser.test_video_recovery as video
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_scribe_capture import capture_page, outage_page

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = ("capture_page", "outage_page")


def test_j4_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("media", "synthetic", "fake capture devices / synthetic media")
    flagship.adapter("video_room", "synthetic", "TELECONSULT_SYNTHETIC_PROVIDER")
    flagship.adapter("signature", "synthetic", "synthetic-signature-v1 HMAC callback")
    flagship.adapter("psp", "synthetic", "recorded-psp-v1 fixture provider")
    for capability in ("video", "qualified_signing", "pix", "asr"):
        flagship.blocked(reference_clinic, capability, "unit_a")


# Launches its own Playwright driver: it runs before any renewal_page user.
def test_j4_interrupted_video_shows_the_recoverable_state(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.interrupted-video",
        "failure",
        video.test_video_recovery_journey,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
        ending="outage",
    )


def test_j4_microphone_denied_keeps_the_manual_path(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.microphone-denied",
        "failure",
        scribe.test_declined_microphone_keeps_manual_clinical_save,
        capture_page=capture_page,
        renewal_base_url=renewal_base_url,
        availability_staff=availability_staff,
    )


def test_j4_capture_outage_pauses_and_never_fabricates(
    outage_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.disconnection",
        "failure",
        scribe.test_seventy_second_outage_pauses_drops_expired_audio_and_records_gaps,
        outage_page=outage_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j4_autosave_survives_offline_and_a_second_tab(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.stale-note-revision",
        "adversarial",
        encounter.test_autosave_survives_a_dropped_save_offline_and_a_second_tab,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j4_finalized_note_changes_only_by_amendment(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.stale-note-revision",
        "failure",
        amendments.test_amendment_journey,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_j4_delayed_signing_never_reads_as_signed(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.delayed-signing",
        "failure",
        prescribing.test_prescribing_recovery_double_submit_and_stale_draft,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j4_resubmitted_payment_opens_no_second_charge(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J4.duplicate-payment",
        "failure",
        billing.test_browser_back_and_resubmit_never_opens_a_second_charge,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j4_duplicate_payment_callback_settles_once(  # noqa: PLR0913 - scenario fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    operations.test_s13_forged_replayed_and_late_callbacks_settle_once(
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        reference_clinic=reference_clinic,
        flagship=flagship,
    )
    flagship.cover("J4.duplicate-payment", "adversarial", settlements=1)
