"""The same bounded assistant under a live portal session, never a staff actor."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import front_desk
from apps.ai.models import FrontDeskThread, FrontDeskTurn
from apps.audit.models import AuditEvent
from apps.comms.inbox_services import open_conversation
from apps.comms.portal_messages import portal_thread, reply_to_portal_conversation
from apps.identity.models import ServicePrincipalGrant
from apps.intake.models import PatientSession
from apps.intake.patient_access import patient_session_context
from django.db import connection

from ai.support import set_policy
from ai.test_front_desk import (
    Desk,
    appointment_start,
    approved_hours,
    desk_world,
    first,
    pt,
    verify,
    when,
)
from ai.test_front_desk import (
    say as inbox_say,
)
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def portal(desk: Desk) -> tuple[UUID, UUID]:
    _, secret = _issue(desk.graph, desk.enrollment)
    session = _redeem(desk.graph.clinic_a, secret)
    assert session is not None
    with as_actor(desk.graph, desk.manager):
        thread = open_conversation(
            clinic_id=desk.graph.clinic_a,
            enrollment_id=desk.enrollment,
            channel="portal",
            purpose="transactional",
        ).conversation_id
    return session, thread


def say(
    desk: Desk, session: UUID, thread: UUID, text: str, key: UUID | None = None
) -> tuple[str, ...]:
    with runtime_role(), patient_session_context(session):
        reply_to_portal_conversation(
            clinic_id=desk.graph.clinic_a,
            conversation_id=thread,
            body=text,
            idempotency_key=key or uuid4(),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('app.current_user_id',true),"
                "current_setting('app.current_tenant',true)"
            )
            assert all(value in (None, "") for value in cursor.fetchone())
        view = portal_thread(clinic_id=desk.graph.clinic_a, conversation_id=thread)
    return tuple(message.body for message in view.messages if message.from_clinic)


def test_verified_portal_books_only_after_accepting_the_exact_offer(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    session, thread = portal(desk)
    original = appointment_start(desk)
    replies = say(desk, session, thread, "Quando é a minha consulta?")
    assert replies == (
        first(pt(front_desk.NEXT_APPOINTMENT) % {"when": when(original, desk.zone)}),
    )
    replies = say(desk, session, thread, "Quero remarcar")
    with owner_context(desk.graph.organization_a):
        state = FrontDeskThread.objects.get(conversation_id=thread)
    assert state.offered_start is not None
    assert appointment_start(desk) == original
    assert replies[-1] == pt(front_desk.OFFER) % {
        "when": when(state.offered_start, desk.zone)
    }
    key = uuid4()
    replies = say(desk, session, thread, "SIM", key)
    assert replies[-1] == pt(front_desk.BOOKED) % {
        "when": when(state.offered_start, desk.zone)
    }
    assert appointment_start(desk) == state.offered_start
    assert say(desk, session, thread, "SIM", key) == replies
    with owner_context(desk.graph.organization_a):
        assert FrontDeskTurn.objects.filter(thread__conversation_id=thread).count() == 3
        assert not PatientSession.objects.filter(
            grant__issued_by_label="front-desk channel"
        ).exists()
        events = list(
            AuditEvent.objects.filter(
                organization_id=desk.graph.organization_a,
                event_type="ai.front_desk.replied",
            )
        )
    assert len(events) == 3
    assert all(event.actor_user_id == session for event in events)
    assert all(
        event.payload
        == {"clinic_id": str(desk.graph.clinic_a), "object_verb": "replied"}
        for event in events
    )


@pytest.mark.parametrize("channel", ["portal", "whatsapp"])
@pytest.mark.parametrize(
    "reply",
    [
        "não confirmo",
        "não aceito",
        "como confirmar?",
        "sim?",
        "sim, mas outro dia",
        "sim não",
    ],
)
def test_negated_questioned_or_qualified_acceptance_never_books(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    reply: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    original = appointment_start(desk)
    if channel == "portal":
        session, thread = portal(desk)
        say(desk, session, thread, "Quero remarcar")
    else:
        verify(desk)
        inbox_say(desk, "Quero remarcar")
    with owner_context(desk.graph.organization_a):
        assert FrontDeskThread.objects.get().offered_start is not None
    if channel == "portal":
        say(desk, session, thread, reply)
    else:
        inbox_say(desk, reply)
    assert appointment_start(desk) == original
    with owner_context(desk.graph.organization_a):
        assert not FrontDeskTurn.objects.filter(
            tool="scheduling.book_approved"
        ).exists()


def test_portal_uses_reviewed_knowledge_and_hands_symptoms_to_a_person(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    article = approved_hours(desk)
    session, thread = portal(desk)
    replies = say(desk, session, thread, "Qual o horário de funcionamento?")
    assert replies == (first(article.body),)
    replies = say(desk, session, thread, "Dor no peito e falta de ar agora")
    assert replies[-1] == pt(front_desk.SAFETY)
    assert say(desk, session, thread, "Quando é minha consulta?") == replies
    with owner_context(desk.graph.organization_a):
        state = FrontDeskThread.objects.get(conversation_id=thread)
    assert (state.state, state.handoff_reason) == ("handoff_pending", "urgent")


def test_portal_without_booking_scope_cannot_gain_it_through_the_agent(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    session, thread = portal(desk)
    with owner_context(desk.graph.organization_a):
        PatientSession.objects.filter(pk=session).update(operations=["messages"])
    original = appointment_start(desk)
    replies = say(desk, session, thread, "Quando é minha consulta?")
    assert replies == (first(pt(front_desk.NOT_VERIFIED)),)
    assert when(original, desk.zone) not in "\n".join(replies)
    assert appointment_start(desk) == original
    with owner_context(desk.graph.organization_a):
        assert (
            not PatientSession.objects.exclude(pk=session)
            .filter(grant__issued_by_label="front-desk channel")
            .exists()
        )


def test_disabled_portal_agent_keeps_the_human_message_path(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    session, thread = portal(desk)
    set_policy(desk.gateway, cap=10_000_000, enabled=False)
    assert say(desk, session, thread, "Quando é minha consulta?") == ()
    with owner_context(desk.graph.organization_a):
        assert not FrontDeskTurn.objects.filter(thread__conversation_id=thread).exists()


@pytest.mark.parametrize("channel", ["portal", "whatsapp"])
def test_revoked_agent_cannot_execute_an_already_offered_move(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    original = appointment_start(desk)
    if channel == "portal":
        session, thread = portal(desk)
        replies = say(desk, session, thread, "Quero remarcar")
    else:
        verify(desk)
        assert inbox_say(desk, "Quero remarcar") is not None
    with owner_context(desk.graph.organization_a):
        ServicePrincipalGrant.objects.filter(
            organization_id=desk.graph.organization_a, permission="agent.execute"
        ).update(active=False)
        before = FrontDeskTurn.objects.count()
    if channel == "portal":
        assert say(desk, session, thread, "SIM") == replies
    else:
        assert inbox_say(desk, "SIM") is None
    assert appointment_start(desk) == original
    with owner_context(desk.graph.organization_a):
        assert FrontDeskTurn.objects.count() == before
