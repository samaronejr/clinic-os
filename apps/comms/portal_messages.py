"""Patient portal messages: the patient side of todo 51's portal threads (MSG-01).

A patient session, or a verified delegate's, that holds the ``messages`` operation
lists the portal threads of its effective enrollment, reads its own side of each
thread and the clinic messages the outbox accepted, and replies in a thread that
is not closed. Every read and write runs through a session-bound SECURITY DEFINER
resolver (``migrations/_portal_messages_sql.py``): a patient request holds no
staff or tenant GUC. Unknown, foreign-patient, foreign-clinic and malformed
selectors share the patient-access denial. A reply is triaged like any inbound
message, so clinical or urgent text reaches the clinician queue; nothing here
answers the patient on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from psycopg import Error as PostgreSQLError

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import AuditTrustedContext, _content_hash
from apps.comms.inbox_rules import MAX_BODY_CHARS
from apps.comms.inbox_services import publish_messages_hint
from apps.comms.triage import classify_inbound
from apps.core.secrets import secret_store
from apps.intake.access import PatientAccessDeniedError
from apps.tenancy.envelope import KEK_SECRET_NAME, protect

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    type _Parameter = str | bytes | UUID | datetime

__all__ = (
    "ESCALATED_EVENT",
    "MESSAGES_OPERATION",
    "REPLY_EVENT",
    "PortalConversation",
    "PortalConversationClosedError",
    "PortalMessage",
    "PortalMessageConflictError",
    "PortalMessageInputError",
    "PortalMessageScope",
    "PortalReplyReceipt",
    "PortalThread",
    "patient_conversations",
    "portal_message_scope",
    "portal_thread",
    "register_urgent_reply_hook",
    "reply_to_portal_conversation",
)

MESSAGES_OPERATION: Final = "messages"
REPLY_EVENT: Final = "comms.portal.reply_received"
ESCALATED_EVENT: Final = "comms.portal.escalated"
_BODY_PURPOSE: Final = "comms.message.body"


class PortalMessageInputError(ValueError):
    """Reject an empty or oversized reply before any write, without echoing it."""

    def __init__(self) -> None:
        """Keep the message free of reply content."""
        super().__init__("invalid portal reply")


class PortalMessageConflictError(Exception):
    """The form key was already used with another text; nothing was stored."""

    def __init__(self) -> None:
        """Expose only the fixed conflict code."""
        super().__init__("portal reply conflict")


class PortalConversationClosedError(Exception):
    """The clinic closed the thread: it stays readable and takes no reply."""

    def __init__(self) -> None:
        """Expose only the fixed state code."""
        super().__init__("conversation closed")


@dataclass(frozen=True, slots=True)
class PortalMessageScope:
    """The live messages scope of this session, re-derived on every call."""

    session_id: UUID
    organization_id: UUID
    clinic_id: UUID
    timezone: str
    acting_for_patient: bool


@dataclass(frozen=True, slots=True)
class PortalConversation:
    """One portal thread of the session subject, without staff-only facts."""

    conversation_id: UUID
    purpose: str
    state: str
    created_at: datetime
    last_message_at: datetime | None
    message_count: int


@dataclass(frozen=True, slots=True)
class PortalMessage:
    """One message the patient may read: its own, or an accepted clinic message."""

    message_id: UUID
    from_clinic: bool
    body: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PortalThread:
    """A thread and its readable messages, oldest first."""

    conversation: PortalConversation
    messages: tuple[PortalMessage, ...]


@dataclass(frozen=True, slots=True)
class PortalReplyReceipt:
    """The stored reply; ``replayed`` marks a repeat of the same form."""

    message_id: UUID
    conversation_id: UUID
    replayed: bool


def _resolver_row(sql: str, parameters: Sequence[_Parameter]) -> tuple[object, ...]:
    """Run one fixed resolver command and map its SQLSTATE to the contract."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            row = cursor.fetchone()
    except DatabaseError as error:
        cause = error.__cause__
        if isinstance(cause, PostgreSQLError):
            if cause.sqlstate == "42501":
                raise PatientAccessDeniedError from error
            if cause.sqlstate == "40001":
                raise PortalMessageConflictError from error
            if cause.sqlstate == "55000":
                raise PortalConversationClosedError from error
            if cause.sqlstate == "22023":
                raise PortalMessageInputError from error
        raise
    if row is None:
        raise PatientAccessDeniedError
    return tuple(row)


def portal_message_scope() -> PortalMessageScope | None:
    """Return the session's messages scope, or None without that operation."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.comms_patient_scope()")
        row = cursor.fetchone()
    if row is None:
        return None
    session_id, organization_id, clinic_id, zone, acting = row
    if not (
        isinstance(session_id, UUID)
        and isinstance(organization_id, UUID)
        and isinstance(clinic_id, UUID)
        and isinstance(zone, str)
        and type(acting) is bool
    ):
        return None
    return PortalMessageScope(session_id, organization_id, clinic_id, zone, acting)


def patient_conversations() -> tuple[PortalConversation, ...]:
    """List the session subject's portal threads, latest message first."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.comms_patient_conversations()")
        rows = cursor.fetchall()
    return tuple(PortalConversation(*row) for row in rows)


def portal_thread(*, clinic_id: UUID, conversation_id: UUID) -> PortalThread:
    """Load one thread of the session subject; any other selector is the denial."""
    scope = portal_message_scope()
    if (
        scope is None
        or type(clinic_id) is not UUID
        or type(conversation_id) is not UUID
        or scope.clinic_id != clinic_id
    ):
        raise PatientAccessDeniedError
    conversation = next(
        (
            row
            for row in patient_conversations()
            if row.conversation_id == conversation_id
        ),
        None,
    )
    if conversation is None:
        raise PatientAccessDeniedError
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.comms_patient_messages(%s,%s)",
            [conversation_id, kek],
        )
        rows = cursor.fetchall()
    return PortalThread(conversation, tuple(PortalMessage(*row) for row in rows))


def _audit(scope: PortalMessageScope, event_name: str, record_id: UUID) -> None:
    """Chain one patient-session event; the payload is clinic id and verb only."""
    event = build_phase1_audit_event(
        event_name, clinic_id=scope.clinic_id, affected_record_id=record_id
    )
    content_hash = _content_hash(
        event.event,
        AuditTrustedContext(
            organization_id=scope.organization_id, actor_user_id=scope.session_id
        ),
        dict(event.payload),
    )
    _resolver_row(
        "SELECT clinic_app.comms_patient_audit(%s,%s,%s,%s)",
        [record_id, event_name, event.event.occurred_at_utc, content_hash],
    )


_URGENT_REPLY_HOOKS: list[Callable[[UUID], None]] = []


def register_urgent_reply_hook(hook: Callable[[UUID], None]) -> None:
    """Route a newly stored urgent reply (its message id) to a human queue.

    Hooks run in the reply's own transaction under the same patient session,
    after the message is stored; a failing hook rolls the reply back.
    """
    if hook not in _URGENT_REPLY_HOOKS:
        _URGENT_REPLY_HOOKS.append(hook)


def _record_reply(  # noqa: PLR0913 - one stored reply's facts
    scope: PortalMessageScope,
    *,
    message_id: UUID,
    conversation_id: UUID,
    changed: bool,
    escalated: bool,
    urgent: bool,
) -> None:
    """Audit a stored reply and route a new urgent one to the human queues."""
    if changed:
        _audit(scope, REPLY_EVENT, message_id)
    if escalated:
        _audit(scope, ESCALATED_EVENT, conversation_id)
    if changed and urgent:
        for hook in _URGENT_REPLY_HOOKS:
            hook(message_id)


def reply_to_portal_conversation(
    *, clinic_id: UUID, conversation_id: UUID, body: str, idempotency_key: UUID
) -> PortalReplyReceipt:
    """Store one reply in an open thread; the same form key returns its receipt.

    The text is encrypted under the tenant envelope before it reaches the
    resolver, triaged like an inbound message and never logged or audited.
    """
    if type(clinic_id) is not UUID or type(conversation_id) is not UUID:
        raise PatientAccessDeniedError
    text = body.strip() if type(body) is str else ""
    if not 0 < len(text) <= MAX_BODY_CHARS or type(idempotency_key) is not UUID:
        raise PortalMessageInputError
    scope = portal_message_scope()
    if scope is None or scope.clinic_id != clinic_id:
        raise PatientAccessDeniedError
    verdict = classify_inbound(text)
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    with transaction.atomic():
        envelope = protect(purpose=_BODY_PURPOSE, plaintext=text.encode("utf-8"))
        message_id, thread_id, changed, escalated = _resolver_row(
            "SELECT * FROM clinic_app.comms_patient_reply(%s,%s,%s,%s,%s,%s)",
            [
                clinic_id,
                conversation_id,
                envelope,
                idempotency_key,
                kek,
                verdict.category,
            ],
        )
        if not (
            isinstance(message_id, UUID)
            and thread_id == conversation_id
            and type(changed) is bool
            and type(escalated) is bool
        ):
            raise PatientAccessDeniedError
        _record_reply(
            scope,
            message_id=message_id,
            conversation_id=conversation_id,
            changed=changed,
            escalated=escalated,
            urgent=verdict.category == "urgent",
        )
    if changed:
        publish_messages_hint(clinic_id)
        if verdict.needs_clinician:
            from apps.workflows.inbox import publish_inbox_hint  # noqa: PLC0415

            publish_inbox_hint(clinic_id)
    return PortalReplyReceipt(message_id, conversation_id, replayed=not changed)
