"""Inbox feed for operational tasks the actor owns, directly or through a role."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import BooleanField, Case, IntegerField, Q, Value, When
from django.db.models.expressions import RawSQL
from django.db.models.signals import post_save
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.identity.current_context import require_permission
from apps.identity.models import UserClinicRole
from apps.workflows.forms import KIND_CHOICES
from apps.workflows.inbox import (
    PRIORITY_RANK,
    InboxItem,
    due_before,
    publish_inbox_hint,
    register_inbox_feed,
)
from apps.workflows.models import Task

if TYPE_CHECKING:
    from apps.workflows.inbox import InboxScope

ACTIVE_STATES = ("open", "assigned", "in_progress")


def task_items(scope: InboxScope) -> tuple[InboxItem, ...]:
    """Return owned active tasks; result review tasks surface as result items.

    Ownership is decided by ``workflows_owned``, the predicate the task row
    policy uses, so a manager's wider row visibility never becomes an inbox.
    Tasks bound to an orders work item are represented by the result feed and
    are excluded here so one result never appears twice. The selected owner
    and due filters narrow the owned rows before the bound.
    """
    require_permission("tasks.view", clinic_id=scope.clinic_id)
    owned = (
        Task.objects.filter(clinic_id=scope.clinic_id, state__in=ACTIVE_STATES)
        .annotate(
            owned=RawSQL(
                "clinic_app.workflows_owned(workflows_task.clinic_id,"
                "workflows_task.owner_user_id,workflows_task.owner_role)",
                (),
                output_field=BooleanField(),
            )
        )
        .filter(
            Q(owned=True)
            | Q(owner_user__isnull=True, owner_role="", created_by_id=scope.actor_id)
        )
        .exclude(resultworkitem__isnull=False)
    )
    if scope.owner == "mine":
        owned = owned.filter(owner_user_id=scope.actor_id)
    cutoff = due_before(scope)
    if cutoff is not None:
        owned = owned.filter(due_at__lt=cutoff)
    rows = owned.order_by(
        Case(
            *(
                When(priority=name, then=Value(rank))
                for name, rank in PRIORITY_RANK.items()
            ),
            default=Value(len(PRIORITY_RANK)),
            output_field=IntegerField(),
        ),
        "due_at",
        "id",
    )[: scope.limit]
    kinds = dict(KIND_CHOICES)
    role_labels = dict(UserClinicRole.Role.choices)
    return tuple(
        InboxItem(
            kind="task",
            record_id=task.pk,
            target_id=task.pk,
            revision=task.revision,
            title=_("Task: %(kind)s") % {"kind": kinds[task.kind]},
            owner=(
                "me"
                if task.owner_user_id == scope.actor_id
                else "role"
                if task.owner_role
                else "unassigned"
            ),
            owner_label=str(role_labels.get(task.owner_role, "")),
            due_at=task.due_at,
            priority=task.priority,
            action="open_task",
        )
        for task in rows
    )


def task_changed(sender: type[Task], instance: Task, **kwargs: object) -> None:
    """Hint subscribed inboxes after a task change commits."""
    del sender, kwargs
    publish_inbox_hint(instance.clinic_id)


def register() -> None:
    """Register the task feed and its commit-bound refetch hint."""
    register_inbox_feed("task", task_items, label=gettext_noop("Tasks"), position=30)
    post_save.connect(task_changed, sender=Task, dispatch_uid="workflows.inbox.task")
