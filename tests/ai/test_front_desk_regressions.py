"""Real-role regressions for the six independent task 54 gate findings."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import front_desk
from apps.ai.front_desk_kb import approve_article, draft_article
from apps.ai.front_desk_staff import resolve_handoff
from apps.ai.models import FrontDeskTurn
from apps.audit.models import AuditEvent
from apps.comms.models import Conversation
from apps.intake.contacts import save_contact_destination, verify_contact
from apps.intake.models import PatientClinicEnrollment, PatientSession
from apps.intake.patient_access import patient_session_context
from apps.intake.services import create_patient
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentLocalRange,
    cancel_appointment,
    create_appointment,
)
from django.urls import reverse

from ai.test_front_desk import (
    BIRTH,
    Desk,
    agent_replies,
    appointment_start,
    approved_hours,
    desk_world,
    first,
    pt,
    say,
    thread_of,
    turns,
    verify,
)
from ai.test_front_desk_portal import portal
from ai.test_front_desk_portal import say as portal_say
from ai.test_front_desk_views import _post
from comms.support import inbox_account, whatsapp_body
from core.test_navigation import _client_for, _get
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def signed_batch(desk: Desk, *messages: str) -> None:
    """Deliver the actual signed provider batch before any runner response."""
    entries = [
        json.loads(desk.inbox.message_body(text=text))["entry"][0]["changes"][0][
            "value"
        ]["messages"][0]
        for text in messages
    ]
    receipt = desk.inbox.receive(
        whatsapp_body(desk.inbox.phone_number_id, messages=entries)
    )
    assert receipt.accepted == len(messages)


def test_t54_g1_signed_batch_cannot_accept_an_unpublished_offer(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    verify(desk)
    original = appointment_start(desk)
    signed_batch(desk, "Quero remarcar", "SIM")
    offered = thread_of(desk).offered_start
    assert offered is not None
    assert appointment_start(desk) == original
    assert turns(desk)[-1].tool == ""
    assert thread_of(desk).state == "active"
    assert say(desk, "SIM") == pt(front_desk.BOOKED) % {
        "when": front_desk._when(offered, desk.zone)
    }
    assert appointment_start(desk) == offered
    assert sum(turn.tool == "scheduling.book_approved" for turn in turns(desk)) == 1


@pytest.mark.parametrize("handoff", ["ATENDENTE", "Dor no peito e falta de ar"])
def test_t54_g2_signed_batch_handoff_invalidates_questions_and_acceptance(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    handoff: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    approved_hours(desk)
    verify(desk)
    say(desk, "Quero remarcar")
    original = appointment_start(desk)
    before = len(agent_replies(rbac_graph))
    signed_batch(desk, handoff, "Qual o horário de funcionamento?", "SIM")
    assert len(agent_replies(rbac_graph)) == before + 1
    assert appointment_start(desk) == original
    stale = turns(desk)[-2:]
    assert all(turn.reply_id is None and turn.tool == "" for turn in stale)
    thread = thread_of(desk)
    assert thread.state == "handoff_pending"
    with as_actor(rbac_graph, desk.manager):
        resolve_handoff(
            clinic_id=rbac_graph.clinic_a,
            thread_id=thread.pk,
            expected_revision=thread.revision,
        )
    assert say(desk, "Oi") == pt(front_desk.GREETING)
    assert len(agent_replies(rbac_graph)) == before + 2
    with owner_context(rbac_graph.organization_a):
        assert not FrontDeskTurn.objects.filter(
            pk__in=[turn.pk for turn in stale], reply__isnull=False
        ).exists()
    assert appointment_start(desk) == original


@pytest.mark.parametrize("after_offer", [False, True])
@pytest.mark.parametrize("verify_new_destination", [False, True])
def test_t54_g3_changed_contact_cannot_mint_or_reuse_channel_authority(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    after_offer: bool,
    verify_new_destination: bool,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    original = appointment_start(desk)
    if after_offer:
        verify(desk)
        say(desk, "Quero remarcar")
    else:
        say(desk, "Quando é minha consulta?")
    session = thread_of(desk).session_id
    with as_actor(rbac_graph, desk.manager):
        save_contact_destination(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=desk.enrollment,
            channel="whatsapp",
            destination="+5511999990002",
            expected_version=1,
        )
        if verify_new_destination:
            verify_contact(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=desk.enrollment,
                channel="whatsapp",
                expected_version=2,
            )
    if session is not None:
        with runtime_role(), patient_session_context(session) as binding:
            assert binding is None
    reply = say(desk, "SIM" if after_offer else "02/01/2000")
    assert reply is not None
    assert reply != pt(front_desk.VERIFIED)
    assert appointment_start(desk) == original
    assert turns(desk)[-1].tool == ""
    assert front_desk._when(original, desk.zone) not in reply
    if not after_offer:
        with owner_context(rbac_graph.organization_a):
            assert not PatientSession.objects.filter(
                grant__issued_by_label="front-desk channel"
            ).exists()


def no_upcoming(desk: Desk, *, available: bool) -> None:
    """Withdraw this patient's appointment; optionally fill the only free slot."""
    with owner_context(desk.graph.organization_a):
        original = Appointment.objects.get(pk=desk.appointment)
    with as_actor(desk.graph, desk.manager):
        cancel_appointment(
            appointment_id=desk.appointment,
            reason=Appointment.CancellationReason.PATIENT_REQUEST,
        )
        if not available:
            other = create_patient(
                clinic_id=desk.graph.clinic_a,
                full_name="Synthetic Other Booking",
                birth_date=BIRTH,
                idempotency_key=uuid4(),
            )
            create_appointment(
                clinic_id=desk.graph.clinic_a,
                enrollment_id=other.enrollment.pk,
                practitioner_id=desk.graph.physician,
                local_range=AppointmentLocalRange(
                    original.start_at.astimezone(desk.zone).strftime("%Y-%m-%dT%H:%M"),
                    original.end_at.astimezone(desk.zone).strftime("%Y-%m-%dT%H:%M"),
                ),
                idempotency_key=uuid4(),
            )


@pytest.mark.parametrize("channel", ["portal", "whatsapp"])
@pytest.mark.parametrize("available", [False, True])
def test_t54_g4_new_booking_requires_available_slot_and_fresh_acceptance(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    available: bool,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch, free=available)
    no_upcoming(desk, available=available)
    if channel == "portal":
        session, conversation = portal(desk)

        def send(text: str) -> str | None:
            return portal_say(desk, session, conversation, text)[-1]

    else:
        verify(desk)

        def send(text: str) -> str | None:
            return say(desk, text)

    reply = send("Quero agendar uma consulta")
    with owner_context(rbac_graph.organization_a):
        offered = FrontDeskTurn.objects.order_by("created_at", "id").last()
        assert offered is not None
        patient_id = PatientClinicEnrollment.objects.get(pk=desk.enrollment).patient_id
        start = offered.thread.offered_start
        assert not Appointment.objects.filter(
            patient_id=patient_id,
            status__in=("scheduled", "requested", "held"),
        ).exists()
    if not available:
        expected = pt(front_desk.NO_SLOTS)
        assert reply == (first(expected) if channel == "portal" else expected)
        assert (offered.tool, offered.outcome, start) == ("", "waitlist", None)
        send("ATENDENTE")
        return
    assert (offered.tool, offered.outcome) == ("scheduling.hold", "offered")
    assert start is not None
    assert front_desk._when(start, desk.zone) in str(reply)
    send("SIM")
    with owner_context(rbac_graph.organization_a):
        booked = Appointment.objects.get(
            patient_id=patient_id,
            status="scheduled",
        )
        accepted = FrontDeskTurn.objects.order_by("created_at", "id").last()
    assert booked.start_at == start
    assert accepted is not None
    assert accepted.tool == "scheduling.book_approved"


def test_t54_g5_handoff_opens_by_csrf_post_without_url_selector(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    say(desk, "ATENDENTE")
    thread = thread_of(desk)
    receptionist, _user = _client_for(rbac_graph, "receptionist")
    path = reverse("comms:inbox", args=(rbac_graph.clinic_a,))
    page = _get(
        receptionist,
        reverse("ai:front-desk-handoffs", args=(rbac_graph.clinic_a,)),
    )
    assert f"/{thread.conversation_id}/" not in page.content.decode()
    selected = {"action": "show", "conversation_id": str(thread.conversation_id)}
    opened = _post(receptionist, path, selected)
    assert opened.status_code == 200
    assert opened.request["PATH_INFO"] == path
    assert not opened.request["QUERY_STRING"]
    assert f'data-conversation="{thread.conversation_id}"' in opened.content.decode()
    assert str(thread.conversation_id) not in json.dumps(dict(receptionist.session))
    handled = _post(
        receptionist,
        path,
        {
            "action": "note",
            "conversation_id": str(thread.conversation_id),
            "body": "Synthetic handoff note",
        },
    )
    assert handled.status_code == 302
    assert handled["Location"] == path
    assert str(thread.conversation_id) not in handled["Location"]
    reopened = _post(receptionist, path, selected)
    assert "Synthetic handoff note" in reopened.content.decode()
    with runtime_role():
        assert receptionist.post(path, selected).status_code == 403
    unknown = _post(
        receptionist, path, {"action": "show", "conversation_id": str(uuid4())}
    )
    foreign_inbox = inbox_account(
        replace(rbac_graph, clinic_a=rbac_graph.clinic_b),
        settings,
        phone_number_id=f"1{uuid4().int % 10**14:014d}",
    )
    foreign_inbox.receive(foreign_inbox.message_body(text="Oi"))
    with owner_context(rbac_graph.organization_a):
        foreign_conversation = Conversation.objects.get(
            provider_account_id=foreign_inbox.account
        )
    foreign = _post(
        receptionist,
        path,
        {"action": "show", "conversation_id": str(foreign_conversation.pk)},
    )
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.content == foreign.content


def test_t54_g6_replacement_approval_audits_retirement_and_approval(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    previous = approved_hours(desk)
    with as_actor(rbac_graph, desk.manager):
        replacement = draft_article(
            clinic_id=rbac_graph.clinic_a,
            key=previous.key,
            topic=previous.topic,
            title=previous.title,
            body="Atendemos de segunda a sábado, das 8h às 18h.",
            keywords=tuple(previous.keywords),
        )
        approve_article(
            clinic_id=rbac_graph.clinic_a,
            article_id=replacement.pk,
            expected_revision=1,
        )
    with owner_context(rbac_graph.organization_a):
        events = list(
            AuditEvent.objects.filter(
                organization_id=rbac_graph.organization_a,
                event_type__in=(
                    "ai.knowledge_article.retired",
                    "ai.knowledge_article.approved",
                ),
            ).order_by("seq")
        )
    assert [(event.event_type, event.affected_record_id) for event in events[-2:]] == [
        ("ai.knowledge_article.retired", str(previous.pk)),
        ("ai.knowledge_article.approved", str(replacement.pk)),
    ]
    assert [event.payload for event in events[-2:]] == [
        {"clinic_id": str(rbac_graph.clinic_a), "object_verb": verb}
        for verb in ("retired", "approved")
    ]
