"""Real-browser inbox journeys: reply, outage, hostile deliveries and entry points.

Beyond the reply and delivery paths it covers the staff entry points: live updates
through the realtime hint, linking a verified number to a patient, starting a care
thread from the care team and quick replies. ``inbox.webm`` records the happy path.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.utils.translation import gettext, pgettext
from playwright.sync_api import expect

from renewal.browser._navigation import click_to_navigate, goto_settled, reload_settled
from renewal.browser._page_wait import wait_for_js
from renewal.browser._teleconsult import register_physician
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.messages_support import (
    CONTACT,
    add_care_team,
    count,
    grant_messages,
    latest_enrollment,
    latest_operation,
    message_body,
    run_worker,
    seed_expired_thread,
    seed_provider,
    seed_quick_reply,
    status_body,
)
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_contacts import _open_contacts, _save_destination, _verify
from renewal.browser.test_passkeys import _organization_admin
from renewal.browser.test_staff_intake import REGISTER, SEARCH_FORM

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
OK = 200
FORBIDDEN = 403
NOT_FOUND = 404
ADMINISTRATIVE = "Qual o horário de funcionamento?"
CLINICAL = "Posso tomar o remédio com café?"


def _capture(page: Page, root: Path, state: str, width: int) -> None:
    directory = root / "messages"
    directory.mkdir(mode=0o700, exist_ok=True)
    page.emulate_media(reduced_motion="reduce")
    full_page_screenshot(page, directory / f"{state}-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _accessibility(page: Page, base: str, root: Path, width: int) -> None:
    check_page(page, base, root, f"messages-{width}")
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    for reflow_width in (640, 320):
        page.set_viewport_size({"width": reflow_width, "height": 900})
        _capture(page, root, f"reflow-from-{width}", reflow_width)


def _inbox(staff: dict[str, str], base: str) -> str:
    return f"{base}/clinics/{staff['clinic_a']}/messages/"


SETTLED_JS = (
    "!document.querySelector('.htmx-request, .htmx-settling, .htmx-added,"
    ' [aria-busy="true"]\')'
)
LIVE_STATES = re.compile("connected|denied|offline")


def _settle_live_region(page: Page) -> None:
    """Wait until the realtime client has decided and the region stopped swapping.

    On load the client either connects or is refused, and each outcome refetches
    the region once; a click that lands during that swap hits a replaced node.
    """
    expect(page.locator("[data-realtime-topic]")).to_have_attribute(
        "data-realtime-state", LIVE_STATES
    )
    wait_for_js(page, SETTLED_JS)


def _open_thread(page: Page, *, contact: str | None = None) -> None:
    """Open a thread from the list; the contact picks the row by its masked number."""
    _settle_live_region(page)
    rows = page.locator("tr[data-conversation]")
    row = rows.filter(has_text=contact).first if contact else rows.first
    link = row.get_by_role("link", name=pgettext("inbox action", "Open"))
    expect(link).to_be_visible()
    click_to_navigate(link)
    expect(page.locator("[data-conversation][data-purpose]")).to_be_visible()


def _reply(page: Page, text: str) -> None:
    page.locator("#reply-body").fill(text)
    click_to_navigate(
        page.get_by_role("button", name=pgettext("inbox", "Send"), exact=True)
    )
    expect(
        page.get_by_role("status").filter(
            has_text=gettext("Message queued for sending.")
        )
    ).to_be_visible()


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_inbound_to_assignment_to_reply_to_delivery(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    provider = seed_provider(staff)
    first = provider.deliver(renewal_page, base, message_body(ADMINISTRATIVE))
    assert first.status == OK
    assert first.json() == {"accepted": 1, "duplicates": 0, "ignored": 0}
    _sign_in_receptionist(renewal_page, base, staff)
    browser = renewal_page.context.browser
    assert browser is not None
    directory = root / "messages"
    directory.mkdir(mode=0o700, exist_ok=True)
    context = browser.new_context(
        storage_state=renewal_page.context.storage_state(),
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": width, "height": 900},
        record_video_dir=str(directory / "video") if width == 1280 else None,
    )
    page = context.new_page()
    try:
        response = goto_settled(page, _inbox(staff, base))
        assert response is not None
        assert response.status == OK
        expect(page.get_by_role("heading", level=1)).to_have_text(gettext("Messages"))
        expect(page.locator("tr[data-conversation]")).to_have_count(1)
        _capture(page, root, "inbox-list", width)
        _open_thread(page)
        expect(page.locator(".thread__item--in")).to_contain_text(ADMINISTRATIVE)
        expect(page.locator('[data-window="open"]')).to_be_visible()
        click_to_navigate(page.get_by_role("button", name=gettext("Assign to me")))
        expect(
            page.get_by_role("status").filter(has_text=gettext("Assignment updated."))
        ).to_be_visible()
        _reply(page, "Atendemos das 8h às 18h, de segunda a sexta.")
        sent = page.locator(".thread__item--out")
        expect(sent).to_have_attribute("data-state", "queued")
        expect(sent).to_contain_text(pgettext("delivery state", "Queued"))
        _capture(page, root, "queued", width)
        operation, _ = latest_operation(staff)
        assert run_worker(staff, operation, "sent", directory) == ["succeeded"]
        reload_settled(page)
        expect(page.locator(".thread__item--out")).to_have_attribute(
            "data-state", "sent"
        )
        _, reference = latest_operation(staff)
        assert reference == f"synthetic:whatsapp:{operation}"
        # Sent is not delivered: only the authenticated provider status says so.
        delivered = provider.deliver(
            renewal_page, base, status_body(reference, "delivered")
        )
        assert delivered.status == OK
        assert provider.deliver(
            renewal_page, base, status_body(reference, "delivered")
        ).json() == {"accepted": 0, "duplicates": 1, "ignored": 0}
        reload_settled(page)
        expect(page.locator(".thread__item--out")).to_have_attribute(
            "data-state", "delivered"
        )
        _capture(page, root, "delivered", width)
        _accessibility(page, base, root, width)
    finally:
        context.close()
        if page.video is not None:
            movie = directory / "inbox.webm"
            page.video.save_as(movie)
            movie.chmod(0o600)


def test_provider_down_shows_not_sent_and_recovery_never_duplicates(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    directory = root / "messages"
    directory.mkdir(mode=0o700, exist_ok=True)
    provider = seed_provider(staff)
    assert provider.deliver(renewal_page, base, message_body(ADMINISTRATIVE)).ok
    _sign_in_receptionist(renewal_page, base, staff)
    page = renewal_page
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    _reply(page, "Atendemos das 8h às 18h.")
    first_operation, _ = latest_operation(staff)
    outcomes = run_worker(staff, first_operation, "fail", directory)
    assert outcomes[-1] == "failed"
    assert "succeeded" not in outcomes
    reload_settled(page)
    expect(page.locator(".thread__item--out")).to_have_attribute("data-state", "failed")
    expect(page.locator(".thread__item--out")).to_contain_text(
        pgettext("delivery state", "Not sent")
    )
    _capture(page, root, "not-sent", 1280)
    retry = page.get_by_role("button", name=gettext("Try sending again"))
    expect(retry).to_be_visible()
    click_to_navigate(retry)
    expect(
        page.get_by_role("status").filter(has_text=gettext("Resend queued."))
    ).to_be_visible()
    second_operation, reference = latest_operation(staff)
    assert second_operation != first_operation
    assert reference is None
    assert run_worker(staff, second_operation, "sent", directory) == ["succeeded"]
    reload_settled(page)
    # One message in the thread, two operations as evidence, one provider reference.
    expect(page.locator(".thread__item--out")).to_have_count(1)
    expect(page.locator(".thread__item--out")).to_have_attribute("data-state", "sent")
    assert count(provider, "comms_message", "direction='out'") == 1
    assert (
        count(
            provider,
            "comms_integrationoperation",
            "subject_type='comms.message' AND provider_reference IS NOT NULL",
        )
        == 1
    )


def test_hostile_deliveries_and_clinical_text_change_nothing(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    provider = seed_provider(staff)
    page = renewal_page
    body = message_body(ADMINISTRATIVE, event="wamid.REPLAY")
    forged = provider.deliver(page, base, body, forged=True)
    assert forged.status == FORBIDDEN
    assert count(provider, "comms_conversation") == 0
    foreign = provider.deliver(
        page, base, message_body(ADMINISTRATIVE, phone="999999999999999")
    )
    assert foreign.json() == {"accepted": 0, "duplicates": 0, "ignored": 1}
    assert count(provider, "comms_conversation") == 0
    stuffed = json.dumps(
        {**json.loads(body), "clinic_id": "00000000-0000-4000-8000-000000000001"}
    ).encode()
    assert provider.deliver(page, base, stuffed).json()["accepted"] == 1
    assert provider.deliver(page, base, stuffed).json()["duplicates"] == 1
    assert count(provider, "comms_conversation") == 1
    assert (
        count(provider, "comms_conversation", f"clinic_id='{staff['clinic_a']}'") == 1
    )
    assert count(provider, "comms_message", "direction='in'") == 1
    clinical = provider.deliver(
        page, base, message_body(CLINICAL, event="wamid.CLINICAL")
    )
    assert clinical.json()["accepted"] == 1
    _sign_in_receptionist(page, base, staff)
    goto_settled(page, _inbox(staff, base))
    expect(page.locator("tr[data-conversation]")).to_have_count(1)
    row = page.locator("tr[data-conversation]").first
    expect(row).to_contain_text(gettext("Clinical question"))
    click_to_navigate(row.get_by_role("link", name=pgettext("inbox action", "Open")))
    expect(
        page.get_by_role(
            "heading", name=gettext("Question forwarded to the clinical team")
        )
    ).to_be_visible()
    expect(
        page.get_by_role("button", name=gettext("Ask for a suggested reply"))
    ).to_have_count(0)
    _capture(page, root, "clinical-flag", 1280)


def test_expired_window_offers_no_free_text(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    provider = seed_provider(staff)
    conversation = seed_expired_thread(staff, provider)
    _sign_in_receptionist(renewal_page, base, staff)
    page = renewal_page
    goto_settled(page, f"{_inbox(staff, base)}{conversation}/")
    expect(page.locator('[data-window="closed"]')).to_be_visible()
    expect(page.locator("#reply-body")).to_have_count(0)
    expect(
        page.get_by_role("button", name=pgettext("inbox", "Send"), exact=True)
    ).to_have_count(0)
    _capture(page, root, "window-expired", 1280)


def _register_patient(page: Page, base: str, staff: dict[str, str], name: str) -> str:
    """Register a patient through the real form and return the new enrollment."""
    patients = f"/intake/clinics/{staff['clinic_a']}/patients/"
    goto_settled(page, f"{base}{patients}")
    click_to_navigate(page.locator(f"{SEARCH_FORM} a.button--secondary"))
    expect(page.locator("h1")).to_have_text(gettext("Register a patient"))
    page.locator("#id_full_name").fill(name)
    page.locator("#id_birth_date").fill("1990-05-17")
    click_to_navigate(page.locator(REGISTER))
    page.wait_for_url(f"**{patients}")
    return latest_enrollment(staff)


def _contact(
    page: Page, base: str, staff: dict[str, str], name: str, *, verified: bool
) -> None:
    """Save the inbound number as the patient's WhatsApp contact, as staff would."""
    _open_contacts(page, base, staff, name)
    _save_destination(page, gettext("WhatsApp"), f"+{CONTACT}")
    if verified:
        _verify(page, gettext("WhatsApp"))


def _new_name() -> str:
    return f"Paciente Sintetico {uuid4().hex[:8]}"


def test_a_new_inbound_message_appears_without_a_reload(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    directory = root / "messages"
    directory.mkdir(mode=0o700, exist_ok=True)
    provider = seed_provider(staff)
    assert provider.deliver(renewal_page, base, message_body(ADMINISTRATIVE)).ok
    operator = _organization_admin(staff)["id"]
    grant_messages(staff, operator, staff["receptionist_id"], directory)
    _sign_in_receptionist(renewal_page, base, staff)
    page = renewal_page
    goto_settled(page, _inbox(staff, base))
    root_element = page.locator("[data-realtime-topic]")
    expect(root_element).to_have_attribute("data-realtime-state", "connected")
    expect(page.locator("tr[data-conversation]")).to_have_count(1)
    # A different number opens a second thread; the list refetches by itself.
    page.evaluate("() => { window.sameDocument = true; }")
    other = provider.deliver(
        page, base, message_body(ADMINISTRATIVE, sender="5511988880001")
    )
    assert other.json()["accepted"] == 1
    expect(page.locator("tr[data-conversation]")).to_have_count(2)
    assert page.evaluate("window.sameDocument") is True
    _capture(page, root, "live-list", 1280)
    _open_thread(page, contact="0001")
    expect(root_element).to_have_attribute("data-realtime-state", "connected")
    # Text typed into the composer survives the thread refetch.
    page.locator("#reply-body").fill("rascunho em andamento")
    page.evaluate("() => { window.sameThread = true; }")
    again = provider.deliver(
        page, base, message_body("Mais uma pergunta?", sender="5511988880001")
    )
    assert again.json()["accepted"] == 1
    expect(page.locator(".thread__item--in")).to_have_count(2)
    assert page.evaluate("window.sameThread") is True
    expect(page.locator("#reply-body")).to_have_value("rascunho em andamento")
    _capture(page, root, "live-thread", 1280)


def test_a_hint_that_lands_while_a_refetch_settles_is_not_lost(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """htmx binds the triggers of swapped-in content only when its settle step runs.

    The settle delay is stretched so a real hint, delivered after the refetch has
    swapped but before it settled, lands inside that window; the thread must still
    end up showing the new message instead of dropping the hint.
    """
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    directory = root / "messages"
    directory.mkdir(mode=0o700, exist_ok=True)
    provider = seed_provider(staff)
    assert provider.deliver(renewal_page, base, message_body(ADMINISTRATIVE)).ok
    operator = _organization_admin(staff)["id"]
    grant_messages(staff, operator, staff["receptionist_id"], directory)
    _sign_in_receptionist(renewal_page, base, staff)
    page = renewal_page
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    expect(page.locator("[data-realtime-topic]")).to_have_attribute(
        "data-realtime-state", "connected"
    )
    _settle_live_region(page)
    page.evaluate(
        "() => { window.hints = 0; window.swaps = 0;"
        " htmx.config.defaultSettleDelay = 3000;"
        " const on = (name, key) => document.body.addEventListener(name,"
        " () => { window[key] += 1; });"
        " on('rt:messages', 'hints'); on('htmx:afterSwap', 'swaps'); }"
    )
    try:
        page.evaluate("() => document.body.dispatchEvent(new CustomEvent('rt:poll'))")
        wait_for_js(page, "window.swaps >= 1")
        assert provider.deliver(page, base, message_body("Mais uma pergunta?")).ok
        wait_for_js(page, "window.hints >= 1")
        expect(page.locator(".thread__item--in")).to_have_count(2)
    finally:
        page.evaluate("() => { htmx.config.defaultSettleDelay = 20; }")
    _capture(page, root, "hint-during-settle", 1280)


def test_reception_links_a_verified_number_to_a_patient(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    page = renewal_page
    provider = seed_provider(staff)
    name = _new_name()
    _sign_in_receptionist(page, base, staff)
    _register_patient(page, base, staff, name)
    _contact(page, base, staff, name, verified=True)
    assert provider.deliver(page, base, message_body(ADMINISTRATIVE)).ok
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    expect(page.locator("[data-link-panel]")).to_be_visible()
    page.locator("#patient-query").fill(name)
    click_to_navigate(page.get_by_role("button", name=gettext("Search patients")))
    candidate = page.locator(".candidate", has_text=name)
    expect(candidate).to_be_visible()
    check_page(page, base, root, "messages-link-panel")
    _capture(page, root, "link-candidates", 1280)
    click_to_navigate(
        candidate.get_by_role(
            "button", name=re.compile(gettext("Link to this patient"))
        )
    )
    expect(
        page.get_by_role("status").filter(
            has_text=gettext("Patient linked through a verified contact.")
        )
    ).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_text(name)
    expect(page.locator("[data-link-panel]")).to_have_count(0)
    assert count(provider, "comms_conversation", "enrollment_id IS NOT NULL") == 1


def test_an_unverified_number_is_refused_and_verification_is_one_click_away(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base = availability_staff, renewal_base_url
    page = renewal_page
    provider = seed_provider(staff)
    name = _new_name()
    _sign_in_receptionist(page, base, staff)
    _register_patient(page, base, staff, name)
    _contact(page, base, staff, name, verified=False)
    assert provider.deliver(page, base, message_body(ADMINISTRATIVE)).ok
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    page.locator("#patient-query").fill(name)
    click_to_navigate(page.get_by_role("button", name=gettext("Search patients")))
    candidate = page.locator(".candidate", has_text=name)
    click_to_navigate(
        candidate.get_by_role(
            "button", name=re.compile(gettext("Link to this patient"))
        )
    )
    expect(
        page.get_by_role("alert").filter(
            has_text=gettext("The patient's contact is not verified.")
        )
    ).to_be_visible()
    assert count(provider, "comms_conversation", "enrollment_id IS NOT NULL") == 0
    page.locator("#patient-query").fill(name)
    click_to_navigate(page.get_by_role("button", name=gettext("Search patients")))
    click_to_navigate(
        page.locator(".candidate", has_text=name).get_by_role(
            "button", name=re.compile(gettext("Check or verify the contact"))
        )
    )
    expect(page.locator("h1")).to_have_text(
        gettext("Contacts and messaging preferences")
    )
    _verify(page, gettext("WhatsApp"))
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    page.locator("#patient-query").fill(name)
    click_to_navigate(page.get_by_role("button", name=gettext("Search patients")))
    click_to_navigate(
        page.locator(".candidate", has_text=name).get_by_role(
            "button", name=re.compile(gettext("Link to this patient"))
        )
    )
    expect(page.get_by_role("heading", level=1)).to_have_text(name)


def test_a_clinician_starts_a_care_thread_that_reception_cannot_open(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    page = renewal_page
    seed_provider(staff)
    name = _new_name()
    _sign_in_receptionist(page, base, staff)
    enrollment = _register_patient(page, base, staff, name)
    _contact(page, base, staff, name, verified=True)
    add_care_team(staff, enrollment, staff["physician_a_id"])
    register_physician(staff)
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    clinician = context.new_page()
    try:
        _sign_in_physician(clinician, base, staff)
        goto_settled(clinician, _inbox(staff, base))
        expect(clinician.locator("[data-start-panel]")).to_be_visible()
        click_to_navigate(
            clinician.get_by_role("button", name=gettext("Show my patients"))
        )
        candidate = clinician.locator(".candidate", has_text=name)
        expect(candidate).to_be_visible()
        candidate.locator("select[name=channel]").select_option("whatsapp")
        candidate.locator("select[name=purpose]").select_option("care")
        click_to_navigate(
            candidate.get_by_role(
                "button", name=re.compile(gettext("Start conversation"))
            )
        )
        expect(
            clinician.locator("[data-conversation][data-purpose=care]")
        ).to_be_visible()
        expect(clinician.locator(".lede")).to_contain_text(
            pgettext("conversation purpose", "Care")
        )
        thread_url = clinician.url
        _capture(clinician, root, "care-thread", 1280)
    finally:
        context.close()
    # Reception holds no care role: the list omits it and the thread is a 404.
    goto_settled(page, _inbox(staff, base))
    expect(page.locator("tr[data-conversation]")).to_have_count(0)
    response = goto_settled(page, thread_url)
    assert response is not None
    assert response.status == NOT_FOUND


def test_quick_replies_fill_the_composer_and_send_nothing(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    page = renewal_page
    provider = seed_provider(staff)
    seed_quick_reply(staff, "Horário", "Atendemos das 8h às 18h, de segunda a sexta.")
    assert provider.deliver(page, base, message_body(ADMINISTRATIVE)).ok
    _sign_in_receptionist(page, base, staff)
    goto_settled(page, _inbox(staff, base))
    _open_thread(page)
    expect(page.locator("[data-quick-replies]")).to_be_visible()
    click_to_navigate(page.get_by_role("button", name="Horário"))
    expect(page.locator("#reply-body")).to_have_value(
        "Atendemos das 8h às 18h, de segunda a sexta."
    )
    assert count(provider, "comms_message", "direction='out'") == 0
    _capture(page, root, "quick-reply", 1280)
