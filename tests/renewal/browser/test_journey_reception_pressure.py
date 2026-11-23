"""J3: reception under pressure (todo 75 ``journey-reception-pressure``).

Several staff handle simultaneous calls and messages, rescheduling, arrivals,
room conflicts and waitlist offers without double-booking, losing task
context or revealing clinical content. Every scene runs concurrently against
the realtime stack (task-owned Redis, SSE stream, same-origin proxy): two
receptionists race the last room slot (Barrier, one booking), two contexts
move one appointment (no lost update), another desk's arrival refetches the
queue and a connected clinician's Today, the keyboard move dialog rejects a
target taken meanwhile, a waitlist offer is accepted by the patient while
staff and patient race the same slot, an inbound WhatsApp message lands in the
staff inbox without a reload and is assigned and answered, offline actions
never claim success, and the waiting-room display shows no patient data.
Each scene is the owning todo's proven journey (21-26, 51), re-run here
side by side on the reference clinic's organization.
"""

# Injects SINTETICO-SENTINELA-* values into protected fields (runner scan).
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

import renewal.browser.test_agenda_grid as grid
import renewal.browser.test_agenda_slice as agenda_slice
import renewal.browser.test_messages as messages
import renewal.browser.test_realtime as realtime
import renewal.browser.test_reception as reception
import renewal.browser.test_self_booking as self_booking
import renewal.browser.test_waitlist as waitlist
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_agenda import agenda_staff
from renewal.browser.test_agenda_grid import (
    board,
    grid_report,
    grid_root,
    grid_state,
)
from renewal.browser.test_agenda_slice import (
    scene,
    signed_in_state,
    slice_report,
    slice_root,
    slice_world,
)
from renewal.browser.test_reception import (
    reception_page,
    reception_world,
    sentinel_booking,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = (
    "agenda_staff",
    "board",
    "grid_report",
    "grid_root",
    "grid_state",
    "grid_world",
    "reception_page",
    "reception_world",
    "scene",
    "sentinel_booking",
    "signed_in_state",
    "slice_report",
    "slice_root",
    "slice_world",
)

# Order matters: the waitlist and reception proofs own clinic A until the agenda
# grid and slice worlds seed their rooms and days later in this session.


@pytest.fixture(scope="session")
def grid_world(agenda_staff: dict[str, str]) -> grid.GridWorld:
    """Use the Monday after reception's fixture week, not the grid's random shift."""
    world = grid.GridWorld(agenda_staff)
    world.next_day = reception.RECEPTION_START_DAY + timedelta(days=8)
    return world


def test_j3_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("realtime", "real", "task-owned Redis, SSE stream and proxy")
    flagship.adapter("whatsapp", "synthetic", "COMMS_SYNTHETIC_CHANNELS webhook HMAC")
    for capability in ("whatsapp", "sms", "email"):
        flagship.blocked(reference_clinic, capability, "unit_a")
    assert len(reference_clinic.rooms["unit_a"]) == 2
    assert len(reference_clinic.personas) == 19


def test_j3_waitlist_offer_reaches_the_patient(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.waitlist-offer",
        "happy",
        waitlist.test_waitlist_journey,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_j3_arrivals_queue_to_room(
    reception_page: Page,
    reception_world: object,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.arrivals",
        "happy",
        reception.test_arrive_queue_call_to_room_with_native_keyboard_forms,
        reception_page=reception_page,
        reception_world=reception_world,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
    )


def test_j3_another_desk_arrival_refetches_the_queue(
    reception_page: Page,
    reception_world: object,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.realtime",
        "happy",
        reception.test_reception_refetches_after_another_desk_registers_arrival,
        reception_page=reception_page,
        reception_world=reception_world,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
    )


def test_j3_offline_desk_actions_never_claim_success(
    reception_page: Page,
    reception_world: object,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.arrivals",
        "failure",
        reception.test_offline_actions_never_claim_success,
        reception_page=reception_page,
        reception_world=reception_world,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
    )


def test_j3_waiting_display_reveals_nothing_sensitive(  # noqa: PLR0913 - owner proof fixtures
    reception_page: Page,
    reception_world: object,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    sentinel_booking: tuple[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.no-clinical-content",
        "adversarial",
        reception.test_display_has_no_phi_and_revocation_matches_forged_token,
        reception_page=reception_page,
        reception_world=reception_world,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        sentinel_booking=sentinel_booking,
    )
    flagship.sentinel("PATIENT")


def test_j3_arrival_refreshes_a_connected_clinician_today(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.realtime",
        "happy",
        realtime.test_arrival_refreshes_a_connected_clinician_today,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        agenda_staff=agenda_staff,
    )


def test_j3_two_receptionists_race_the_last_room_slot(
    board: object, flagship: FlagshipReport
) -> None:
    rerun(
        flagship,
        "J3.room-conflict",
        "adversarial",
        grid.test_two_receptionists_racing_the_last_room_slot_hold_it_once,
        board=board,
    )


def test_j3_keyboard_reschedule_with_a_target_taken_meanwhile(
    board: object, flagship: FlagshipReport
) -> None:
    rerun(
        flagship,
        "J3.reschedule",
        "failure",
        grid.test_a_target_taken_meanwhile_leaves_the_original_unchanged,
        board=board,
    )


def test_j3_other_unit_busy_time_reveals_no_patient(
    board: object, flagship: FlagshipReport
) -> None:
    rerun(
        flagship,
        "J3.no-clinical-content",
        "adversarial",
        grid.test_other_unit_busy_time_names_no_patient,
        board=board,
    )


def test_j3_two_contexts_move_one_appointment_without_a_lost_update(
    scene: object, flagship: FlagshipReport
) -> None:
    rerun(
        flagship,
        "J3.concurrent-staff",
        "adversarial",
        agenda_slice.test_two_contexts_moving_one_appointment_lose_no_update,
        variant="htmx",
        scene=scene,
    )


def test_j3_staff_and_patient_race_one_slot(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.waitlist-offer",
        "adversarial",
        self_booking.test_staff_patient_race,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )


def test_j3_inbound_message_is_assigned_and_answered(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.messages",
        "happy",
        messages.test_inbound_to_assignment_to_reply_to_delivery,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=375,
    )


def test_j3_new_inbound_appears_without_losing_context(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J3.messages",
        "adversarial",
        messages.test_a_new_inbound_message_appears_without_a_reload,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
    )
