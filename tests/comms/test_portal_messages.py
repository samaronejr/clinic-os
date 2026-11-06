"""Patient portal messages on real PostgreSQL roles (todo 50 over todo 51 threads).

A patient session, or a verified delegate's that holds the ``messages`` operation,
reads the clinic messages the outbox accepted and replies in a portal thread that
is not closed, through session-bound resolvers. Closed threads, other patients,
other clinics, staff contexts, sessions without the operation and delegates
without the scope fail closed; unknown and foreign selectors share one denial,
and no refusal writes a row or an audit event.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms import inbox_queries
from apps.comms import inbox_services as services
from apps.comms.inbox_rules import MAX_BODY_CHARS
from apps.comms.models import Conversation, IntegrationOperation, Message
from apps.comms.portal_messages import (
    PortalConversationClosedError,
    PortalMessageConflictError,
    PortalMessageInputError,
    PortalReplyReceipt,
    patient_conversations,
    portal_message_scope,
    portal_thread,
    reply_to_portal_conversation,
)
from apps.core.integration import execute_operation
from apps.identity.delegates import activate, revoke, switch_delegate
from apps.identity.delegates import verify_delegate_grant as verify
from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    issue_invitation,
    patient_session_context,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone
from psycopg import Error as PostgreSQLError

from comms.support import manager_of
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context
from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from patients.delegate_support import active_grant, delegate_world, request_grant
from renewal.test_patient_access import _issue, _redeem, _seed_patients

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from apps.intake.patient_access import PatientSessionBinding

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SENTINEL = "SINTETICO-SENTINELA resposta do portal"
STAFF_TEXT = "Sua consulta de amanhã está confirmada."
URL = "/patient/messages/"
OK = 200
BAD_REQUEST = 400
FORBIDDEN = 403
CONFLICT = 409


@dataclass(frozen=True)
class Portal:
    """Ana in clinic A and Bia in clinic B, each with a live all-operation session."""

    enrollment: UUID
    session: UUID
    other_enrollment: UUID
    other_session: UUID


def _portal(graph: RbacGraph) -> Portal:
    access = _seed_patients(graph)
    _, secret = _issue(graph, access.enrollment_id)
    session = _redeem(graph.clinic_a, secret)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        issued = issue_invitation(
            clinic_id=graph.clinic_b, enrollment_id=access.other_enrollment_id
        )
    other = _redeem(graph.clinic_b, issued.secret)
    assert session is not None
    assert other is not None
    return Portal(access.enrollment_id, session, access.other_enrollment_id, other)


def _clinic_a_patient(graph: RbacGraph, name: str) -> tuple[UUID, UUID]:
    """One more clinic-A patient with a live session: a same-clinic neighbour."""
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = create_patient(
            clinic_id=graph.clinic_a,
            full_name=name,
            birth_date=date(1988, 2, 3),
            idempotency_key=uuid4(),
        ).enrollment.pk
    _, secret = _issue(graph, enrollment)
    session = _redeem(graph.clinic_a, secret)
    assert session is not None
    return enrollment, session


@contextmanager
def _as_patient(session: UUID) -> Iterator[PatientSessionBinding | None]:
    with runtime_role(), patient_session_context(session) as binding:
        yield binding


def _thread(graph: RbacGraph, clinic: UUID, enrollment: UUID, actor: UUID) -> UUID:
    with as_actor(graph, actor):
        return services.open_conversation(
            clinic_id=clinic,
            enrollment_id=enrollment,
            channel="portal",
            purpose="transactional",
        ).conversation_id


def _queue(
    graph: RbacGraph, clinic: UUID, conversation: UUID, actor: UUID, text: str
) -> UUID:
    with as_actor(graph, actor):
        return services.send_message(
            clinic_id=clinic,
            conversation_id=conversation,
            command_id=uuid4(),
            body=text,
        ).message_id


def _run(graph: RbacGraph, message: UUID) -> None:
    """Run the message's outbox operation, as the worker would, to acceptance."""
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(subject_id=message)
    with runtime_role():
        assert execute_operation(operation.pk) == "succeeded"


def _accepted(
    graph: RbacGraph, clinic: UUID, conversation: UUID, actor: UUID, text: str
) -> UUID:
    message = _queue(graph, clinic, conversation, actor, text)
    _run(graph, message)
    return message


def _stored(graph: RbacGraph, message: UUID) -> tuple[bytes, object, object, object]:
    """The raw row: envelope bytes, event id, sender and command id."""
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT body, event_id, sent_by_id, command_id "
            "FROM clinic_app.comms_message WHERE id=%s AND direction='in'",
            [message],
        )
        row = cursor.fetchone()
    assert row is not None
    return bytes(row[0]), row[1], row[2], row[3]


def _staff_bodies(graph: RbacGraph, actor: UUID, conversation: UUID) -> list[str]:
    with as_actor(graph, actor):
        view = inbox_queries.conversation_view(
            clinic_id=graph.clinic_a, conversation_id=conversation, now=timezone.now()
        )
    return [row.message.body for row in view.messages]


def _revision(graph: RbacGraph, conversation: UUID) -> int:
    with owner_context(graph.organization_a):
        return Conversation.objects.get(pk=conversation).revision


def _set_state(graph: RbacGraph, conversation: UUID, state: str) -> None:
    with as_actor(graph, manager_of(graph)):
        services.set_conversation_state(
            clinic_id=graph.clinic_a,
            conversation_id=conversation,
            state=state,
            expected_revision=_revision(graph, conversation),
        )


def _rows(graph: RbacGraph) -> list[tuple[object, ...]]:
    """Every message of this graph's organization; census oracles share one DB."""
    with owner_context(graph.organization_a):
        return list(
            Message.objects.filter(organization_id=graph.organization_a)
            .order_by("created_at", "id")
            .values_list("pk", "conversation_id", "direction", "event_id")
        )


def _portal_audit(graph: RbacGraph) -> list[tuple[str, UUID, str, dict[str, str]]]:
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT event_type, actor_user_id, affected_record_id, payload "
            "FROM clinic_app.audit_event WHERE event_type LIKE 'comms.portal.%%' "
            "AND organization_id=%s ORDER BY seq",
            [graph.organization_a],
        )
        return [
            (row[0], row[1], row[2], json.loads(row[3])) for row in cursor.fetchall()
        ]


def _forged_inbound(graph: RbacGraph, conversation: UUID) -> str:
    """Insert an inbound portal row directly; return the SQLSTATE that refused it."""
    with pytest.raises(DatabaseError) as refused, transaction.atomic():
        Message.objects.create(
            organization_id=graph.organization_a,
            conversation_id=conversation,
            direction="in",
            body="Forjada fora da sessão do paciente",
            event_id=f"portal:{uuid4()}",
            created_at=timezone.now(),
        )
    cause = refused.value.__cause__
    assert isinstance(cause, PostgreSQLError)
    return str(cause.sqlstate)


def _patient_client(session: UUID) -> Client:
    client = Client()
    stored = client.session
    stored[PATIENT_SESSION_KEY] = str(session)
    stored.save()
    return client


def test_patient_reads_accepted_clinic_messages_and_replies_once(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    queued = _queue(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    with _as_patient(portal.session):
        scope = portal_message_scope()
        assert scope is not None
        assert (scope.clinic_id, scope.acting_for_patient) == (graph.clinic_a, False)
        (row,) = patient_conversations()
        # A queued clinic message is not accepted yet, so the patient sees none.
        assert (row.conversation_id, row.state, row.message_count) == (
            thread,
            "open",
            0,
        )
        assert row.last_message_at is None
        assert (
            portal_thread(clinic_id=graph.clinic_a, conversation_id=thread).messages
            == ()
        )
    _run(graph, queued)
    key = uuid4()
    reply = {
        "clinic_id": graph.clinic_a,
        "conversation_id": thread,
        "idempotency_key": key,
    }
    with _as_patient(portal.session):
        shown = portal_thread(clinic_id=graph.clinic_a, conversation_id=thread)
        assert [(m.message_id, m.from_clinic, m.body) for m in shown.messages] == [
            (queued, True, STAFF_TEXT)
        ]
        receipt = reply_to_portal_conversation(
            body=f"  {SENTINEL}: qual o horário?  ", **reply
        )
    assert not receipt.replayed
    events, rows = _portal_audit(graph), _rows(graph)
    with _as_patient(portal.session):
        again = reply_to_portal_conversation(
            body=f"{SENTINEL}: qual o horário?", **reply
        )
        with pytest.raises(PortalMessageConflictError):
            reply_to_portal_conversation(body="Outro texto com a mesma chave", **reply)
        thread_view = portal_thread(clinic_id=graph.clinic_a, conversation_id=thread)
        (listed,) = patient_conversations()
    assert again == PortalReplyReceipt(receipt.message_id, thread, replayed=True)
    assert (_rows(graph), _portal_audit(graph)) == (rows, events)
    assert [(m.from_clinic, m.body) for m in thread_view.messages] == [
        (True, STAFF_TEXT),
        (False, f"{SENTINEL}: qual o horário?"),
    ]
    assert (listed.message_count, listed.last_message_at) == (
        2,
        thread_view.messages[-1].created_at,
    )
    assert events == [
        (
            "comms.portal.reply_received",
            portal.session,
            str(receipt.message_id),
            {"clinic_id": str(graph.clinic_a), "object_verb": "received"},
        )
    ]
    body, event_id, sent_by, command = _stored(graph, receipt.message_id)
    assert SENTINEL.encode() not in body
    assert (event_id, sent_by, command) == (f"portal:{key}", None, None)
    with owner_context(graph.organization_a):
        # Portal threads open no WhatsApp service window.
        assert Conversation.objects.get(pk=thread).last_inbound_at is None
    assert _staff_bodies(graph, staff, thread) == [
        STAFF_TEXT,
        f"{SENTINEL}: qual o horário?",
    ]


def test_a_closed_thread_stays_readable_and_refuses_a_new_reply(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    _accepted(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    key = uuid4()
    with _as_patient(portal.session):
        first = reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Obrigado, até amanhã.",
            idempotency_key=key,
        )
    _set_state(graph, thread, "closed")
    rows, events = _rows(graph), _portal_audit(graph)
    with _as_patient(portal.session):
        (row,) = patient_conversations()
        assert row.state == "closed"
        assert (
            len(
                portal_thread(clinic_id=graph.clinic_a, conversation_id=thread).messages
            )
            == 2
        )
        # The form that was already sent still replays to its receipt.
        assert reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Obrigado, até amanhã.",
            idempotency_key=key,
        ) == PortalReplyReceipt(first.message_id, thread, replayed=True)
        with pytest.raises(PortalConversationClosedError):
            reply_to_portal_conversation(
                clinic_id=graph.clinic_a,
                conversation_id=thread,
                body="Mais uma pergunta depois do encerramento",
                idempotency_key=uuid4(),
            )
    assert _rows(graph) == rows
    assert _portal_audit(graph) == events


def test_a_pending_thread_reopens_and_a_clinical_reply_reaches_a_clinician(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    _accepted(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    _set_state(graph, thread, "pending")
    with _as_patient(portal.session):
        administrative = reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Qual o horário de funcionamento?",
            idempotency_key=uuid4(),
        )
    with owner_context(graph.organization_a):
        conversation = Conversation.objects.get(pk=thread)
    assert (conversation.state, conversation.needs_clinician) == ("open", False)
    with _as_patient(portal.session):
        clinical = reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Estou com dor no peito desde ontem.",
            idempotency_key=uuid4(),
        )
        reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Ainda dói um pouco.",
            idempotency_key=uuid4(),
        )
    with owner_context(graph.organization_a):
        conversation = Conversation.objects.get(pk=thread)
    assert (conversation.needs_clinician, conversation.triage_category) == (
        True,
        "urgent",
    )
    events = _portal_audit(graph)
    assert [(event[0], event[2]) for event in events[:3]] == [
        ("comms.portal.reply_received", str(administrative.message_id)),
        ("comms.portal.reply_received", str(clinical.message_id)),
        ("comms.portal.escalated", str(thread)),
    ]
    # The thread is escalated once; a later clinical reply only adds a message.
    assert [event[0] for event in events].count("comms.portal.escalated") == 1
    assert {key for event in events for key in event[3]} == {"clinic_id", "object_verb"}
    assert "peito" not in json.dumps([event[3] for event in events])


def test_other_patients_other_clinics_and_unknown_threads_share_one_denial(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    neighbour, neighbour_session = _clinic_a_patient(graph, "Carla Synthetic Portal")
    own = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    beside = _thread(graph, graph.clinic_a, neighbour, staff)
    foreign = _thread(
        graph, graph.clinic_b, portal.other_enrollment, graph.clinic_admin
    )
    _accepted(graph, graph.clinic_a, own, staff, STAFF_TEXT)
    _accepted(graph, graph.clinic_a, beside, staff, STAFF_TEXT)
    _accepted(graph, graph.clinic_b, foreign, graph.clinic_admin, STAFF_TEXT)
    rows, events = _rows(graph), _portal_audit(graph)
    denials: set[str] = set()
    sqlstates: set[str | None] = set()
    with _as_patient(portal.session):
        assert [row.conversation_id for row in patient_conversations()] == [own]
        for clinic, selector in (
            (graph.clinic_a, beside),
            (graph.clinic_a, foreign),
            (graph.clinic_b, foreign),
            (graph.clinic_b, own),
            (graph.clinic_a, uuid4()),
        ):
            with pytest.raises(PatientAccessDeniedError) as read:
                portal_thread(clinic_id=clinic, conversation_id=selector)
            with pytest.raises(PatientAccessDeniedError) as write:
                reply_to_portal_conversation(
                    clinic_id=clinic,
                    conversation_id=selector,
                    body="Tentativa em outra conversa",
                    idempotency_key=uuid4(),
                )
            denials |= {str(read.value), str(write.value)}
            # Called directly, the definer refuses on its own, before it uses the
            # envelope and without the Python checks or the message guard.
            with pytest.raises(DatabaseError) as direct, transaction.atomic():
                connection.cursor().execute(
                    "SELECT * FROM clinic_app.comms_patient_reply(%s,%s,%s,%s,%s,%s)",
                    [clinic, selector, b"x", uuid4(), "k" * 64, "unclassified"],
                )
            cause = direct.value.__cause__
            assert isinstance(cause, PostgreSQLError)
            sqlstates.add(cause.sqlstate)
        # The reader is keyed by thread alone and returns nothing of another one.
        for selector in (beside, foreign, uuid4()):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM clinic_app.comms_patient_messages(%s,%s)",
                    [selector, "k" * 64],
                )
                assert cursor.fetchall() == []
    assert len(denials) == 1
    assert sqlstates == {"42501"}
    # Each thread stays visible to its own patient: the denial is about scope.
    with _as_patient(neighbour_session):
        assert [row.conversation_id for row in patient_conversations()] == [beside]
    with _as_patient(portal.other_session):
        assert [row.conversation_id for row in patient_conversations()] == [foreign]
    # Staff contexts carry no patient authority, through Python or SQL.
    with as_actor(graph, staff):
        assert portal_message_scope() is None
        assert patient_conversations() == ()
        with pytest.raises(PatientAccessDeniedError):
            portal_thread(clinic_id=graph.clinic_a, conversation_id=own)
        with pytest.raises(DatabaseError) as direct, transaction.atomic():
            connection.cursor().execute(
                "SELECT * FROM clinic_app.comms_patient_reply(%s,%s,%s,%s,%s,%s)",
                [graph.clinic_a, own, b"x", uuid4(), "k" * 64, "unclassified"],
            )
    cause = direct.value.__cause__
    assert isinstance(cause, PostgreSQLError)
    assert cause.sqlstate == "42501"
    assert _rows(graph) == rows
    assert _portal_audit(graph) == events


def test_the_database_refuses_inbound_portal_rows_outside_the_patient_session(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    rows = _rows(graph)
    with as_actor(graph, staff):
        staff_refusal = _forged_inbound(graph, thread)
    with owner_context(graph.organization_a):
        owner_refusal = _forged_inbound(graph, thread)
    assert (staff_refusal, owner_refusal) == ("42501", "42501")
    assert _rows(graph) == rows


def test_sessions_and_delegates_without_the_messages_scope_see_no_thread(
    rbac_graph: RbacGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    world = delegate_world(graph, tmp_path)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, world.notes.enrollment_id, staff)
    _accepted(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    monkeypatch.setattr(
        "apps.intake.patient_access.PATIENT_OPERATION_VALUES",
        ["enrollment_view", "booking"],
    )
    _, secret = _issue(graph, world.notes.enrollment_id)
    limited = _redeem(graph.clinic_a, secret)
    assert limited is not None
    rows = _rows(graph)
    with _as_patient(limited):
        assert portal_message_scope() is None
        assert patient_conversations() == ()
        with pytest.raises(PatientAccessDeniedError):
            portal_thread(clinic_id=graph.clinic_a, conversation_id=thread)
        with pytest.raises(PatientAccessDeniedError):
            reply_to_portal_conversation(
                clinic_id=graph.clinic_a,
                conversation_id=thread,
                body="Sem a operação de mensagens",
                idempotency_key=uuid4(),
            )
    # A verified delegate whose grant lacks the messages operation.
    unscoped = active_grant(graph, world)
    with _as_patient(world.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=unscoped.grant_id)
    with _as_patient(world.session_id):
        assert portal_message_scope() is None
        with pytest.raises(PatientAccessDeniedError):
            portal_thread(clinic_id=graph.clinic_a, conversation_id=thread)
        switch_delegate(clinic_id=graph.clinic_a, grant_id=None)
    with permission_context(graph, world.verifier_id):
        revoke(
            clinic_id=graph.clinic_a,
            grant_id=unscoped.grant_id,
            expected_revision=unscoped.revision,
        )
    # The messages operation without the care purpose grants nothing either.
    purposeless = active_grant(
        graph,
        world,
        operations=("enrollment_view", "messages"),
        purposes=("teleconsultation",),
    )
    with _as_patient(world.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=purposeless.grant_id)
    with _as_patient(world.session_id):
        assert portal_message_scope() is None
        with pytest.raises(PatientAccessDeniedError):
            reply_to_portal_conversation(
                clinic_id=graph.clinic_a,
                conversation_id=thread,
                body="Sem a finalidade de cuidado",
                idempotency_key=uuid4(),
            )
    assert _rows(graph) == rows


def test_a_verified_delegate_with_message_scope_reads_and_replies_for_the_patient(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    world = delegate_world(graph, tmp_path)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, world.notes.enrollment_id, staff)
    _accepted(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    pending = request_grant(
        graph,
        world,
        operations=("enrollment_view", "messages"),
        purposes=("care_processing",),
    )
    # Unverified: the representation cannot be chosen and the thread stays hidden.
    with _as_patient(world.session_id), pytest.raises(PatientAccessDeniedError):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=pending.grant_id)
    with _as_patient(world.session_id):
        assert patient_conversations() == ()
        with pytest.raises(PatientAccessDeniedError):
            portal_thread(clinic_id=graph.clinic_a, conversation_id=thread)
    with permission_context(graph, world.verifier_id):
        verified = verify(
            clinic_id=graph.clinic_a,
            grant_id=pending.grant_id,
            expected_revision=pending.revision,
            verified=True,
        )
        grant = activate(
            clinic_id=graph.clinic_a,
            grant_id=verified.grant_id,
            expected_revision=verified.revision,
        )
    with _as_patient(world.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=grant.grant_id)
    with _as_patient(world.session_id):
        scope = portal_message_scope()
        assert scope is not None
        assert scope.acting_for_patient
        assert [row.conversation_id for row in patient_conversations()] == [thread]
        assert [
            m.body
            for m in portal_thread(
                clinic_id=graph.clinic_a, conversation_id=thread
            ).messages
        ] == [STAFF_TEXT]
        receipt = reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Sou o responsável e confirmo a consulta.",
            idempotency_key=uuid4(),
        )
    (event,) = _portal_audit(graph)
    assert event[:3] == (
        "comms.portal.reply_received",
        world.session_id,
        str(receipt.message_id),
    )
    with permission_context(graph, world.verifier_id):
        revoke(
            clinic_id=graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
        )
    # Revocation denies the already-minted session on its next request.
    with _as_patient(world.session_id) as binding:
        assert binding is None
        assert portal_message_scope() is None
        with pytest.raises(PatientAccessDeniedError):
            portal_thread(clinic_id=graph.clinic_a, conversation_id=thread)


def test_empty_oversized_and_keyless_replies_refuse_before_any_write(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    rows, events = _rows(graph), _portal_audit(graph)
    with _as_patient(portal.session):
        for body, key in (
            ("", uuid4()),
            ("   \n  ", uuid4()),
            ("x" * (MAX_BODY_CHARS + 1), uuid4()),
            ("Texto válido", "not-a-uuid"),
        ):
            with pytest.raises(PortalMessageInputError):
                reply_to_portal_conversation(
                    clinic_id=graph.clinic_a,
                    conversation_id=thread,
                    body=body,
                    idempotency_key=key,  # type: ignore[arg-type]
                )
        with pytest.raises(PatientAccessDeniedError):
            reply_to_portal_conversation(
                clinic_id=graph.clinic_a,
                conversation_id=str(thread),  # type: ignore[arg-type]
                body="Texto válido",
                idempotency_key=uuid4(),
            )
        accepted = reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="x" * MAX_BODY_CHARS,
            idempotency_key=uuid4(),
        )
    assert _rows(graph)[: len(rows)] == rows
    assert [row[0] for row in _rows(graph)[len(rows) :]] == [accepted.message_id]
    assert len(_portal_audit(graph)) == len(events) + 1


def test_messages_page_opens_and_replies_with_selectors_only_in_post_bodies(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    _accepted(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    client = _patient_client(portal.session)
    key = uuid4()
    reply = {
        "action": "reply",
        "conversation_id": str(thread),
        "idempotency_key": str(key),
        "body": SENTINEL,
    }
    with runtime_role():
        home = client.get("/patient/")
        listing = client.get(URL)
        opened = client.post(URL, {"action": "open", "conversation_id": str(thread)})
        sent = client.post(URL, reply)
        reloaded = client.post(URL, reply)
    assert home.status_code == OK
    assert b'data-portal-destination="messages"' in home.content
    assert listing.status_code == OK
    assert "no-store" in listing["Cache-Control"]
    page = listing.content.decode()
    assert f'name="conversation_id" value="{thread}"' in page
    assert f"/{thread}" not in page
    assert f"?conversation_id={thread}" not in page
    assert opened.status_code == OK
    assert STAFF_TEXT in opened.content.decode()
    assert b"data-portal-reply-form" in opened.content
    for response in (sent, reloaded):
        assert response.status_code == OK
        assert b"data-portal-reply-sent" in response.content
        assert response.content.decode().count(SENTINEL) == 1
    assert sum(1 for row in _rows(graph) if row[2] == "in") == 1
    staff_client, _ = receptionist_client(graph)
    with runtime_role():
        staff_page = staff_client.get(f"/clinics/{graph.clinic_a}/messages/{thread}/")
    assert staff_page.status_code == OK
    assert SENTINEL in staff_page.content.decode()


def test_messages_page_refusals_are_one_gate_and_a_closed_thread_keeps_the_text(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    portal = _portal(graph)
    staff = manager_of(graph)
    thread = _thread(graph, graph.clinic_a, portal.enrollment, staff)
    foreign = _thread(
        graph, graph.clinic_b, portal.other_enrollment, graph.clinic_admin
    )
    _accepted(graph, graph.clinic_a, thread, staff, STAFF_TEXT)
    client = _patient_client(portal.session)
    with runtime_role():
        empty = client.post(
            URL,
            {
                "action": "reply",
                "conversation_id": str(thread),
                "idempotency_key": str(uuid4()),
                "body": "   ",
            },
        )
    assert empty.status_code == BAD_REQUEST
    assert b"data-focus-error" in empty.content
    assert b"data-portal-reply-form" in empty.content
    _set_state(graph, thread, "closed")
    rows, events = _rows(graph), _portal_audit(graph)
    with runtime_role():
        closed = client.post(
            URL,
            {
                "action": "reply",
                "conversation_id": str(thread),
                "idempotency_key": str(uuid4()),
                "body": SENTINEL,
            },
        )
        refusals = [
            client.post(URL, body)
            for body in (
                {"action": "open", "conversation_id": str(foreign)},
                {"action": "open", "conversation_id": str(uuid4())},
                {"action": "open", "conversation_id": "not-a-uuid"},
                {"action": "open"},
                {
                    "action": "reply",
                    "conversation_id": str(foreign),
                    "idempotency_key": str(uuid4()),
                    "body": SENTINEL,
                },
                {
                    "action": "reply",
                    "conversation_id": str(foreign),
                    "idempotency_key": "not-a-uuid",
                    "body": SENTINEL,
                },
                {"action": "erase", "conversation_id": str(thread)},
            )
        ]
    assert closed.status_code == CONFLICT
    page = closed.content.decode()
    assert "data-portal-thread-closed" in page
    assert "data-portal-reply-form" not in page
    assert "data-portal-unsent" in page
    assert SENTINEL in page
    assert [response.status_code for response in refusals] == [FORBIDDEN] * 7
    assert len({response.content for response in refusals}) == 1
    assert all("Set-Cookie" not in response.headers for response in refusals)
    assert SENTINEL.encode() not in refusals[4].content
    assert _rows(graph) == rows
    assert _portal_audit(graph) == events
