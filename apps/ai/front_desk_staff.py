"""Staff side of the front-desk agent (todo 54): the handoff queue.

A thread the agent handed off (urgent or clinical text, a request for a person,
no reviewed answer, a failed identity check) waits for a person. Staff who read
the clinic's patients (``demographics.read``) see the waiting threads, answer in
the conversation itself and mark the handoff resolved; the agent then answers
that conversation again only after its next inbound message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from apps.ai.front_desk_kb import FrontDeskAccessDeniedError, FrontDeskConflictError
from apps.ai.front_desk_models import FrontDeskThread
from apps.audit.services import record_phase1_event
from apps.identity.current_context import require_permission

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

__all__ = ("HandoffRow", "handoff_threads", "resolve_handoff")


@dataclass(frozen=True, slots=True)
class HandoffRow:
    """One waiting thread: reason and time only; the text stays in comms."""

    thread_id: UUID
    conversation_id: UUID
    reason: str
    revision: int
    updated_at: datetime


def handoff_threads(*, clinic_id: UUID) -> tuple[HandoffRow, ...]:
    """List the clinic's threads waiting for a person, oldest first."""
    require_permission("demographics.read", clinic_id=clinic_id)
    return tuple(
        HandoffRow(
            row.pk,
            row.conversation_id,
            row.handoff_reason,
            row.revision,
            row.updated_at,
        )
        for row in FrontDeskThread.objects.filter(
            clinic_id=clinic_id, state="handoff_pending"
        ).order_by("updated_at", "pk")[:100]
    )


@transaction.atomic
def resolve_handoff(
    *, clinic_id: UUID, thread_id: UUID, expected_revision: int
) -> HandoffRow:
    """Close a handoff once a person answered in the conversation."""
    actor = require_permission("demographics.read", clinic_id=clinic_id)
    thread = (
        FrontDeskThread.objects.select_for_update()
        .filter(pk=thread_id, clinic_id=clinic_id)
        .first()
    )
    if thread is None:
        raise FrontDeskAccessDeniedError
    if thread.revision != expected_revision or thread.state != "handoff_pending":
        raise FrontDeskConflictError
    thread.state = "resolved"
    thread.resolved_by_id = actor
    thread.revision += 1
    thread.save(update_fields=("state", "resolved_by", "revision", "updated_at"))
    record_phase1_event(
        "ai.front_desk.resolved", clinic_id=clinic_id, affected_record_id=thread.pk
    )
    return HandoffRow(
        thread.pk,
        thread.conversation_id,
        thread.handoff_reason,
        thread.revision,
        thread.updated_at,
    )
