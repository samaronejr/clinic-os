"""Front-desk agent turns on the inbox channels (todo 54, AI-04).

One turn answers one stored inbound message of a clinic whose ``front_desk``
A2 policy is enabled. The authenticated inbound transaction enqueues the turn;
after it commits, ``run_pending_turns`` re-enters the same authenticated
principal context and decides with a closed, deterministic classifier. Message
text is untrusted data: it selects an intent from fixed word lists and never
names a tool, a record or an argument, so "cancel all appointments" calls
nothing. Urgent or clinical text gets fixed safety text and a human handoff (the
comms triage already put the thread in the clinician queue); there is no
clinical advice. Appointment data needs a verified identity: a thread linked to
the patient through a verified contact plus a date-of-birth challenge; the
challenge mints a booking-only patient session, and every appointment read or
change runs under that session through the patient booking services, so real
availability and the scheduling guards decide. Booking happens only after the
patient's explicit acceptance of the exact offered slot. Replies are stored in
the thread as the agent's messages; on the synthetic channels delivery is
simulated, and a real provider send stays BLOCKED-ON-EG.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.db import connection
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.ai.front_desk_kb import ArticleAnswer, best_answer, fold
from apps.comms.inbox_inbound import inbound_context
from apps.comms.triage import classify_inbound
from apps.core.secrets import secret_store
from apps.intake.patient_access import patient_session_context
from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentTerminalError,
    SlotConflictError,
)
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.patient_booking import (
    SLOT_SALT,
    patient_appointments,
    patient_slots,
    reschedule_patient_appointment,
)
from apps.tenancy.envelope import KEK_SECRET_NAME, protect

if TYPE_CHECKING:
    from collections.abc import Sequence

    from apps.scheduling.models import Appointment

__all__ = (
    "Decision",
    "classify",
    "enqueue_turn",
    "run_pending_turns",
)

LANGUAGE: Final = "pt-br"
SEARCH_DAYS: Final = 14
# Matches the challenge bound in ``_front_desk_sql``: three wrong dates end the
# agent's appointment help in this conversation; the team takes over.
MAX_ATTEMPTS: Final = 3
RUNNER_LOCK: Final = "clinic-os:ai:front-desk-runner:v1"
# Rounds of pending-turn reads per delivery; later turns wait for the next one.
MAX_ROUNDS: Final = 3
# The tool each verified appointment intent needs from the enabled policy.
_TOOL_FOR: Final = {
    "reschedule": "scheduling.hold",
    "accept": "scheduling.book_approved",
}
_TURN_NAMESPACE: Final = uuid5(NAMESPACE_URL, "clinic-os:ai:front-desk:v1")
_BODY_PURPOSE: Final = "comms.message.body"
_HUMAN: Final = frozenset(
    {"atendente", "humano", "humana", "pessoa", "recepcao", "recepcionista"}
)
_ACCEPT: Final = frozenset({"sim", "confirmo", "aceito", "confirmar"})
_DECLINE: Final = frozenset({"nao", "outro", "outra"})
_RESCHEDULE: Final = frozenset({"remarcar", "reagendar", "remarcacao", "adiar"})
_APPOINTMENTS: Final = frozenset(
    {"consulta", "consultas", "agendamento", "agendamentos", "agendada", "marcada"}
)
_GREETING: Final = frozenset({"oi", "ola", "bom", "boa", "dia", "tarde", "noite"})
_BIRTH: Final = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_WORD: Final = re.compile(r"[a-z0-9]+")

DISCLOSURE: Final = gettext_noop(
    "You are talking to the clinic's automated assistant. It answers "
    "administrative questions only. To talk to a person, reply ATENDENTE."
)
# EG-3: clinical-owner approval of this safety text is pending; until then it
# only tells the patient to call the clinic or emergency services.
SAFETY: Final = gettext_noop(
    "I cannot assess symptoms. In an emergency, call 192 now; otherwise call "
    "the clinic. Your message was passed to the clinical team."
)
UNKNOWN: Final = gettext_noop(
    "I don't know how to answer that; I am forwarding your message to the clinic team."
)
HUMAN: Final = gettext_noop(
    "I am forwarding this conversation to the clinic team; a person will reply here."
)
GREETING: Final = gettext_noop(
    "I can help with opening hours, prices, services, exam preparation and your "
    "appointments."
)
CHALLENGE: Final = gettext_noop(
    "To protect your data, reply with your date of birth (DD/MM/YYYY)."
)
RETRY: Final = gettext_noop(
    "I could not confirm your identity. Reply with your date of birth (DD/MM/YYYY)."
)
NOT_VERIFIED: Final = gettext_noop(
    "I could not confirm your identity, so I cannot show appointments here. I "
    "am forwarding this conversation to the clinic team."
)
VERIFIED: Final = gettext_noop(
    "Thank you, your identity is confirmed. Ask about your appointment again, or "
    "reply REMARCAR to look for another time."
)
NEXT_APPOINTMENT: Final = gettext_noop(
    "Your next appointment is on %(when)s. Reply REMARCAR to look for another time."
)
NO_APPOINTMENT: Final = gettext_noop(
    "I found no upcoming appointment. To book one, reply ATENDENTE."
)
OFFER: Final = gettext_noop(
    "The next available time is %(when)s. Reply SIM to move your appointment "
    "there, or NÃO to keep it."
)
NO_SLOTS: Final = gettext_noop(
    "There is no available time in the next two weeks. The clinic keeps a "
    "waiting list: reply ATENDENTE and the team will add you to it."
)
BOOKED: Final = gettext_noop("Done: your appointment is now on %(when)s.")
TAKEN: Final = gettext_noop(
    "That time was just taken. Reply REMARCAR to look for another one."
)
KEPT: Final = gettext_noop("All right, your appointment stays as it was.")


@dataclass(frozen=True, slots=True)
class Context:
    """The claimed turn, read through the principal-bound resolver."""

    turn_id: UUID
    channel: str
    thread_state: str
    verified: bool
    session_id: UUID | None
    challenge_pending: bool
    attempts: int
    offered_appointment: UUID | None
    offered_practitioner: UUID | None
    offered_start: datetime | None
    first_turn: bool
    text: str
    tools: tuple[str, ...]
    budget_left: int


@dataclass(frozen=True, slots=True)
class Decision:
    """What one turn does: a closed intent, an optional tool and the reply."""

    intent: str
    outcome: str
    reply: str
    tool: str = ""
    article: UUID | None = None
    handoff: str = ""
    # The held offer: the appointment to move, its practitioner and the slot.
    offer: tuple[UUID, UUID, datetime] | None = None


def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(fold(text)))


def classify(text: str, *, challenge_pending: bool, offer_pending: bool) -> str:
    """Pick one closed intent from fixed word lists; text never names a tool."""
    verdict = classify_inbound(text)
    words = _words(text)
    rules = (
        (verdict.category == "urgent", "urgent"),
        (verdict.needs_clinician, "clinical"),
        (bool(words & _HUMAN), "human"),
        (challenge_pending and _BIRTH.search(text) is not None, "birth_date"),
        (offer_pending and bool(words & _ACCEPT), "accept"),
        (offer_pending and bool(words & _DECLINE), "decline"),
        (bool(words & _RESCHEDULE), "reschedule"),
        (bool(words & _APPOINTMENTS), "appointments"),
        (bool(words) and words <= _GREETING, "greeting"),
    )
    return next((intent for matched, intent in rules if matched), "information")


def _birth(text: str) -> str | None:
    """Return the ISO date the patient typed, or None for an impossible date."""
    match = _BIRTH.search(text)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _when(moment: datetime, zone: ZoneInfo) -> str:
    """Format a moment in the clinic's own time zone."""
    return formats.date_format(moment.astimezone(zone), "SHORT_DATETIME_FORMAT")


def enqueue_turn(message_id: UUID, channel: str) -> None:
    """Inbound hook: in the authenticated inbound transaction, queue one turn.

    Agent replies are simulated deliveries, so only a synthetic channel in
    synthetic mode gets a turn; a real provider channel stays BLOCKED-ON-EG.
    """
    if (
        settings.CLINIC_DATA_MODE != "synthetic"
        or channel not in settings.COMMS_SYNTHETIC_CHANNELS
    ):
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_front_desk_enqueue(%s)", [message_id])


def _context(turn_id: UUID, kek: str) -> Context | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.ai_front_desk_turn(%s,%s)", [turn_id, kek]
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return Context(
        turn_id=row[0],
        channel=row[3],
        thread_state=row[5],
        verified=row[6] is True,
        session_id=row[7],
        challenge_pending=row[8] is True,
        attempts=int(row[9]),
        offered_appointment=row[10],
        offered_practitioner=row[11],
        offered_start=row[12],
        first_turn=row[13] is True,
        text=str(row[14]),
        tools=tuple(row[16]),
        budget_left=int(row[17]),
    )


def _articles(turn_id: UUID) -> tuple[ArticleAnswer, ...]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.ai_front_desk_articles(%s)", [turn_id])
        rows = cursor.fetchall()
    return tuple(
        ArticleAnswer(row[0], row[1], row[2], row[3], row[4], tuple(row[5]), row[6])
        for row in rows
    )


def _state(  # noqa: PLR0913 - one bookkeeping change and its terms
    turn_id: UUID,
    change: str,
    *,
    appointment: UUID | None = None,
    practitioner: UUID | None = None,
    start: datetime | None = None,
    reason: str = "",
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_front_desk_state(%s,%s,%s,%s,%s,%s)",
            [turn_id, change, appointment, practitioner, start, reason],
        )


def _verify(turn_id: UUID, birth: str | None, kek: str) -> tuple[bool, bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.ai_front_desk_verify(%s,%s,%s)",
            [turn_id, birth, kek],
        )
        row = cursor.fetchone()
    return (row is not None and row[0] is True, row is not None and row[2] is True)


def _information(ctx: Context) -> Decision:
    answer = best_answer(_articles(ctx.turn_id), ctx.text)
    if answer is None:
        return Decision("information", "handoff", _(UNKNOWN), handoff="kb_missing")
    # The approved link is a tool use: it needs the policy and budget left.
    link = (
        answer.link != ""
        and "comms.send_approved_link" in ctx.tools
        and ctx.budget_left > 0
    )
    reply = f"{answer.body}\n{answer.link}" if link else answer.body
    return Decision(
        "information",
        "answered",
        reply,
        tool="comms.send_approved_link" if link else "",
        article=answer.article_id,
    )


def _identity(ctx: Context, intent: str, kek: str) -> Decision | None:
    """Open or continue the challenge; None means the thread is verified."""
    if ctx.verified:
        return None
    if ctx.attempts >= MAX_ATTEMPTS:
        return Decision(
            intent, "not_verified", _(NOT_VERIFIED), handoff="verification_failed"
        )
    if intent == "birth_date":
        verified, exhausted = _verify(ctx.turn_id, _birth(ctx.text), kek)
        if verified:
            return Decision(intent, "verified", _(VERIFIED))
        if exhausted:
            return Decision(
                intent, "not_verified", _(NOT_VERIFIED), handoff="verification_failed"
            )
        return Decision(intent, "challenge", _(RETRY))
    if not ctx.challenge_pending:
        _state(ctx.turn_id, "challenge")
    return Decision(intent, "challenge", _(CHALLENGE))


_FIXED: Final = {
    "urgent": (SAFETY, "urgent"),
    "clinical": (SAFETY, "clinical"),
    "human": (HUMAN, "human_request"),
}


def _decide(ctx: Context, kek: str) -> Decision:
    """Decide one turn inside the principal context; appointments come later."""
    offer = ctx.offered_start is not None and ctx.verified
    intent = classify(
        ctx.text, challenge_pending=ctx.challenge_pending, offer_pending=offer
    )
    if intent in _FIXED:
        reply, reason = _FIXED[intent]
        return Decision(intent, "handoff", _(reply), handoff=reason)
    if intent == "greeting":
        return Decision(intent, "answered", _(GREETING))
    if intent == "information":
        return _information(ctx)
    # Verified: the appointment step runs under the patient session.
    return _identity(ctx, intent, kek) or Decision(intent, "", "")


def _slot_practitioner(token: str) -> UUID:
    """Return the practitioner a slot token names; tokens are our own values."""
    return UUID(signing.loads(token, salt=SLOT_SALT)["practitioner"])


def _next_slot(appointment: Appointment, zone: ZoneInfo) -> datetime | None:
    """Return the first real slot of the same practitioner within two weeks."""
    today = timezone.now().astimezone(zone).date()
    for offset in range(1, SEARCH_DAYS + 1):
        for slot in patient_slots(today + timedelta(days=offset), appointment.pk):
            if (
                slot.start_at != appointment.start_at
                and _slot_practitioner(slot.token) == appointment.practitioner_id
            ):
                return slot.start_at
    return None


def _upcoming(appointments: Sequence[Appointment]) -> Appointment | None:
    now = timezone.now()
    future = [
        item
        for item in appointments
        if item.start_at > now and item.status == "scheduled"
    ]
    return future[0] if future else None


def _appointment_step(ctx: Context, decision: Decision) -> Decision:
    """Run the verified appointment intent under the minted patient session."""
    if ctx.session_id is not None:
        with patient_session_context(ctx.session_id) as binding:
            if binding is not None:
                return _under_session(ctx, decision)
    return Decision(
        decision.intent,
        "not_verified",
        _(NOT_VERIFIED),
        handoff="verification_failed",
    )


def _under_session(ctx: Context, decision: Decision) -> Decision:
    zone = ZoneInfo(require_patient_booking_scope().timezone)
    if decision.intent == "accept":
        return _book(ctx, zone)
    if decision.intent == "decline":
        return Decision("decline", "answered", _(KEPT))
    appointment = _upcoming(patient_appointments())
    if appointment is None:
        return Decision(decision.intent, "answered", _(NO_APPOINTMENT))
    if decision.intent != "reschedule":
        when = _when(appointment.start_at, zone)
        return Decision(
            decision.intent, "answered", _(NEXT_APPOINTMENT) % {"when": when}
        )
    start = _next_slot(appointment, zone)
    if start is None:
        return Decision("reschedule", "waitlist", _(NO_SLOTS))
    return Decision(
        "reschedule",
        "offered",
        _(OFFER) % {"when": _when(start, zone)},
        tool="scheduling.hold",
        offer=(appointment.pk, appointment.practitioner_id, start),
    )


def _book(ctx: Context, zone: ZoneInfo) -> Decision:
    """Book exactly the offered slot, rechecked against real availability."""
    if ctx.offered_start is None or ctx.offered_appointment is None:
        return Decision("accept", "refused", _(TAKEN))
    local = ctx.offered_start.astimezone(zone)
    for slot in patient_slots(local.date(), ctx.offered_appointment):
        if (
            slot.start_at == local
            and _slot_practitioner(slot.token) == ctx.offered_practitioner
        ):
            try:
                moved = reschedule_patient_appointment(
                    ctx.offered_appointment, slot.token
                )
            except (
                AppointmentAvailabilityError,
                AppointmentTerminalError,
                SlotConflictError,
            ):
                # Taken or no longer movable since the offer: nothing changed.
                break
            return Decision(
                "accept",
                "booked",
                _(BOOKED) % {"when": _when(moved.start_at, zone)},
                tool="scheduling.book_approved",
            )
    return Decision("accept", "refused", _(TAKEN))


def _finish(ctx: Context, decision: Decision, kek: str) -> None:
    reply = decision.reply
    if ctx.first_turn:
        reply = f"{_(DISCLOSURE)}\n{reply}"
    envelope = protect(purpose=_BODY_PURPOSE, plaintext=reply.encode("utf-8"))
    if decision.handoff:
        _state(ctx.turn_id, "handoff", reason=decision.handoff)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_front_desk_finish(%s,%s,%s,%s,%s,%s,%s)",
            [
                ctx.turn_id,
                envelope,
                kek,
                decision.intent,
                decision.tool,
                decision.outcome,
                decision.article,
            ],
        )


def _run(account_id: UUID, proof: str, turn_id: UUID, kek: str) -> None:
    with inbound_context(account_id=account_id, proof=proof):
        ctx = _context(turn_id, kek)
        if ctx is None:
            return
        decision = _decide(ctx, kek)
        tool = _TOOL_FOR.get(decision.intent)
        if (
            not decision.outcome
            and tool is not None
            and (tool not in ctx.tools or ctx.budget_left <= 0)
        ):
            # The policy does not allow this step now: a person takes over.
            decision = Decision(
                decision.intent, "handoff", _(HUMAN), handoff="policy_limit"
            )
        if decision.outcome:
            _finish(ctx, decision, kek)
            return
    # A verified appointment intent: patient authority, then record the turn.
    decision = _appointment_step(ctx, decision)
    with inbound_context(account_id=account_id, proof=proof):
        ctx = _context(turn_id, kek)
        if ctx is None:
            return
        if decision.offer is not None:
            appointment, practitioner, start = decision.offer
            _state(
                ctx.turn_id,
                "offer",
                appointment=appointment,
                practitioner=practitioner,
                start=start,
            )
        elif decision.intent in {"accept", "decline"}:
            _state(ctx.turn_id, "clear")
        _finish(ctx, decision, kek)


def _pending(account_id: UUID, proof: str) -> list[UUID]:
    with (
        inbound_context(account_id=account_id, proof=proof),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT * FROM clinic_app.ai_front_desk_pending()")
        return [row[0] for row in cursor.fetchall()]


def run_pending_turns(account_id: UUID, proof: str) -> None:
    """After the inbound commit: answer every queued turn of this account.

    One runner per provider account at a time (a session advisory lock held
    across the turn's transactions), so two concurrent deliveries never work
    on the same turn; a waiting runner then finds only the turns left.
    """
    key = f"{RUNNER_LOCK}:{account_id}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", [key])
    try:
        for _round in range(MAX_ROUNDS):
            turns = _pending(account_id, proof)
            if not turns:
                return
            kek = secret_store().get_secret(KEK_SECRET_NAME)
            with translation.override(LANGUAGE):
                for turn_id in turns:
                    _run(account_id, proof, turn_id, kek)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [key])
