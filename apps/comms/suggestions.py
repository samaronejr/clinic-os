"""Reviewable AI reply drafts (A1): shown to a human, never sent on their own.

A draft exists only for text the triage positively calls administrative, only
when an approved drafter is registered, and only if the draft itself carries no
clinical content. Using a draft is an ordinary ``send_message`` command with its
own idempotency key, issued by a signed-in human, so there is no code path from
a model result to a provider call. The governed gateway wiring (purpose
``front_desk`` through ``apps.ai``) is BLOCKED-ON-EG: until a drafter is
registered the panel reads "Sugestão indisponível" and staff continue manually.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.comms.inbox_rules import MAX_BODY_CHARS
from apps.comms.inbox_services import (
    InboxAccessDeniedError,
    InboxRefusedError,
    Reason,
    SendReceipt,
    _conversation,
    send_message,
)
from apps.comms.models import Message, SuggestedReply
from apps.comms.triage import classify_inbound, clinical_content_in_reply

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

MAX_DRAFTERS: Final = 4
MAX_NAME_CHARS: Final = 32


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """What a drafter may read: one administrative inbound message, nothing else."""

    clinic_id: UUID
    conversation_id: UUID
    channel: str
    inbound_text: str


type Drafter = Callable[[DraftRequest], str | None]
_DRAFTERS: list[tuple[str, Drafter]] = []


def register_reply_drafter(name: str, drafter: Drafter) -> None:
    """Register an approved drafter; names are short fixed labels, not content."""
    if (
        len(_DRAFTERS) >= MAX_DRAFTERS
        or not name.isidentifier()
        or len(name) > MAX_NAME_CHARS
    ):
        raise ValueError(name)
    if all(existing != name for existing, _ in _DRAFTERS):
        _DRAFTERS.append((name, drafter))


def request_suggestion(*, clinic_id: UUID, conversation_id: UUID) -> UUID:
    """Draft one reply for the latest administrative inbound message, or refuse."""
    conversation, _ = _conversation(clinic_id, conversation_id, lock=True)
    if conversation.purpose == "marketing" or conversation.needs_clinician:
        raise InboxRefusedError(Reason.AI_NOT_ALLOWED)
    inbound = (
        Message.objects.filter(conversation=conversation, direction="in")
        .order_by("-created_at", "-id")
        .first()
    )
    if inbound is None or not inbound.supported:
        raise InboxRefusedError(Reason.AI_NOT_ALLOWED)
    live = SuggestedReply.objects.filter(in_reply_to=inbound, state="proposed").first()
    if live is not None:
        return live.pk
    if not classify_inbound(inbound.body).allow_ai_suggestion:
        raise InboxRefusedError(Reason.AI_NOT_ALLOWED)
    if not _DRAFTERS:
        raise InboxRefusedError(Reason.AI_UNAVAILABLE)
    name, drafter = _DRAFTERS[0]
    draft = drafter(
        DraftRequest(
            clinic_id=clinic_id,
            conversation_id=conversation_id,
            channel=conversation.channel,
            inbound_text=inbound.body,
        )
    )
    text = draft.strip() if isinstance(draft, str) else ""
    if not 0 < len(text) <= MAX_BODY_CHARS or clinical_content_in_reply(text):
        raise InboxRefusedError(Reason.AI_DROPPED)
    suggestion = SuggestedReply.objects.create(
        organization_id=conversation.organization_id,
        conversation=conversation,
        in_reply_to=inbound,
        body=text,
        drafter=name,
    )
    return suggestion.pk


def _suggestion(clinic_id: UUID, suggestion_id: UUID) -> SuggestedReply:
    suggestion = (
        SuggestedReply.objects.select_for_update()
        .select_related("conversation")
        .filter(
            pk=suggestion_id,
            conversation__clinic_id=clinic_id,
            state="proposed",
        )
        .first()
    )
    if suggestion is None:
        raise InboxAccessDeniedError
    return suggestion


def use_suggestion(
    *,
    clinic_id: UUID,
    suggestion_id: UUID,
    command_id: UUID,
    body: str,
) -> SendReceipt:
    """Send the human-reviewed text; ``body`` is what the reviewer finally chose."""
    with transaction.atomic():
        suggestion = _suggestion(clinic_id, suggestion_id)
        receipt = send_message(
            clinic_id=clinic_id,
            conversation_id=suggestion.conversation_id,
            command_id=command_id,
            body=body,
        )
        _, actor = _conversation(clinic_id, suggestion.conversation_id)
        suggestion.state = "used" if body.strip() == suggestion.body else "edited"
        suggestion.decided_by_id = actor
        suggestion.used_message_id = receipt.message_id
        suggestion.save(update_fields=["state", "decided_by_id", "used_message_id"])
        record_phase1_event(
            "comms.suggestion.decided",
            clinic_id=clinic_id,
            affected_record_id=suggestion.pk,
        )
    return receipt


def discard_suggestion(*, clinic_id: UUID, suggestion_id: UUID) -> None:
    """Reject a draft without penalty; nothing is sent."""
    with transaction.atomic():
        suggestion = _suggestion(clinic_id, suggestion_id)
        _, actor = _conversation(clinic_id, suggestion.conversation_id)
        suggestion.state = "discarded"
        suggestion.decided_by_id = actor
        suggestion.save(update_fields=["state", "decided_by_id"])
        record_phase1_event(
            "comms.suggestion.decided",
            clinic_id=clinic_id,
            affected_record_id=suggestion.pk,
        )
