"""Clinical Inbox feed for patient questions routed to a clinician."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.comms.inbox_queries import row_label
from apps.comms.inbox_services import CLINICIAN_ROLES
from apps.comms.models import Conversation
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import UserClinicRole
from apps.workflows.inbox import InboxItem, due_before, register_inbox_feed

if TYPE_CHECKING:
    from apps.workflows.inbox import InboxScope


def question_items(scope: InboxScope) -> tuple[InboxItem, ...]:
    """Return open threads flagged for a clinician that this actor may handle.

    Unlinked threads are visible to any clinician of the clinic; care threads
    additionally need patient-scoped clinical authority. A thread has no due
    time, so a due filter matches none.
    """
    if (
        due_before(scope) is not None
        or not UserClinicRole.objects.filter(
            user_id=scope.actor_id, clinic_id=scope.clinic_id, role__in=CLINICIAN_ROLES
        ).exists()
    ):
        return ()
    queryset = Conversation.objects.filter(
        clinic_id=scope.clinic_id, needs_clinician=True
    ).exclude(state="closed")
    if scope.owner == "mine":
        queryset = queryset.filter(assignee_id=scope.actor_id)
    items: list[InboxItem] = []
    for conversation in queryset.select_related("enrollment__patient").order_by(
        "last_activity_at", "pk"
    )[: scope.limit]:
        if conversation.purpose == "care":
            try:
                require_permission(
                    "clinical.read",
                    clinic_id=scope.clinic_id,
                    patient_enrollment_id=conversation.enrollment_id,
                )
            except CurrentActorError:
                continue
        urgent = conversation.triage_category == "urgent"
        items.append(
            InboxItem(
                kind="question",
                record_id=conversation.pk,
                revision=conversation.revision,
                title=_("Patient question awaiting a clinician"),
                patient_label=row_label(conversation),
                priority="urgent" if urgent else "high",
                critical=urgent,
                action="open_conversation",
            )
        )
    return tuple(items)


def register_question_feed() -> None:
    """Register the feed; the board resolves its ``open_conversation`` action."""
    register_inbox_feed(
        "question",
        question_items,
        label=gettext_noop("Patient questions"),
        position=40,
    )
