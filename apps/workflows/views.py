"""Operations tasks: native forms with HTMX enhancement and inert refusals."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.workflows.access import owner_matches, require_clinic_access, require_manager
from apps.workflows.forms import (
    KIND_CHOICES,
    PRIORITY_CHOICES,
    REFERENCE_CHOICES,
    STATE_CHOICES,
    CreateTaskForm,
    TaskFilterForm,
)
from apps.workflows.models import Task, WorkflowRun
from apps.workflows.services import (
    WorkflowConflictError,
    WorkflowInputError,
    list_tasks,
)
from apps.workflows.task_commands import _submit

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse, HttpResponseBase

    from apps.workflows.reassignment import ReassignmentPreview


def _staff_catalog(clinic_id: UUID) -> tuple[tuple[UUID, str], ...]:
    require_permission("tasks.view", clinic_id=clinic_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT user_id,display_label FROM clinic_app.workflows_staff_catalog(%s)",
            [clinic_id],
        )
        return tuple((row[0], row[1]) for row in cursor.fetchall())


def _manager(clinic_id: UUID) -> bool:
    try:
        require_manager(clinic_id=clinic_id)
    except CurrentActorError:
        return False
    return True


def _task_row(task: Task, staff: Mapping[UUID, str]) -> dict[str, object]:
    roles = dict(UserClinicRole.Role.choices)
    return {
        "record": task,
        "kind": dict(KIND_CHOICES)[task.kind],
        "state": dict(STATE_CHOICES)[task.state],
        "priority": dict(PRIORITY_CHOICES)[task.priority],
        "owner": staff.get(task.owner_user_id, _("Unassigned"))
        if task.owner_user_id is not None
        else roles.get(task.owner_role, _("Unassigned")),
        "owned": owner_matches(task),
        "active": task.state not in {"done", "cancelled"},
        "comments": tuple(task.comments.order_by("-created_at")[:20]),
        "comment_key": uuid4(),
    }


def _preview_label(
    preview: ReassignmentPreview | None, staff: Mapping[UUID, str]
) -> str:
    if preview is None:
        return ""
    if preview.owner.user_id is not None:
        # preview_reassignment admitted the owner through workflows_owner_valid,
        # the same active clinic-member set workflows_staff_catalog lists; a
        # missing label is an invariant break and fails closed (500, rollback).
        return staff[preview.owner.user_id]
    return str(UserClinicRole.Role(preview.owner.role).label)


def tasks(
    request: HttpRequest, clinic_id: UUID, *, exceptions: bool = False
) -> HttpResponseBase:
    """Decide clinic authority before auth decorators or any shell/session work.

    Over HTTP the workspace registry's pre-view gate (todo 13) has already
    answered an actor without tasks.view with the shared 403 page, so this
    decision only keeps the order for every other caller and fails closed.
    """
    try:
        require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
        return _authorized_tasks(request, clinic_id, exceptions=exceptions)
    except CurrentActorError:
        return render(request, "403.html", status=403)


def _safe_queue(clinic_id: UUID, *, exceptions: bool = False) -> str:
    return reverse(
        "workflows:exceptions" if exceptions else "workflows:tasks", args=(clinic_id,)
    )


@privileged_totp_required(_safe_queue)
def _authorized_tasks(
    request: HttpRequest, clinic_id: UUID, *, exceptions: bool = False
) -> HttpResponse:
    clinic, actor = require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
    if request.method not in {"GET", "POST"}:
        return render(request, "403.html", status=403)
    manager = _manager(clinic_id)
    capabilities = set()
    for permission in ("tasks.assign", "tasks.complete"):
        try:
            require_permission(permission, clinic_id=clinic_id)
        except CurrentActorError:
            continue
        capabilities.add(permission)
    create_form = CreateTaskForm(initial={"subject_id": clinic_id})
    filter_form = TaskFilterForm(
        request.POST if request.POST.get("action") == "filter" else None,
        prefix="filter",
    )
    preview = None
    state, status = "default", 200
    message = ""
    if request.method == "POST":
        try:
            create_form, preview, state, status, message = _submit(
                request, clinic, actor
            )
        except CurrentActorError:
            return render(request, "403.html", status=403)
        except WorkflowConflictError:
            state, status, message = (
                "conflict",
                409,
                _("The task changed. Review the current version and try again."),
            )
        except WorkflowInputError:
            state, status, message = "error", 400, _("Review the highlighted fields.")
    filters = (
        filter_form.cleaned_data
        if filter_form.is_bound and filter_form.is_valid()
        else {}
    )
    tasks = list_tasks(clinic_id=clinic_id, exceptions=exceptions, **filters)
    staff = dict(_staff_catalog(clinic_id))
    preview_owner_label = _preview_label(preview, staff)
    runs = (
        WorkflowRun.objects.filter(clinic_id=clinic_id)
        .filter(
            Q(state="failed")
            | Q(
                steps__error_code__in=(
                    "operation_unknown",
                    "operation_failed",
                    "handler_rejected",
                )
            )
            | Q(
                state__in=("pending", "running", "waiting"),
                updated_at__lt=timezone.now() - timedelta(seconds=60),
            )
        )
        .distinct()
        .select_related("definition_version")
        .order_by("created_at")[:100]
        if exceptions
        else ()
    )
    context = {
        "clinic": clinic,
        "task_rows": tuple(_task_row(task, staff) for task in tasks),
        "create_form": create_form,
        "filter_form": filter_form,
        "manager": manager,
        "can_assign": "tasks.assign" in capabilities,
        "can_complete": "tasks.complete" in capabilities,
        "preview": preview,
        "preview_owner_label": preview_owner_label,
        "date_state": "error" if create_form.errors else "default",
        "staff": tuple(staff.items()),
        "roles": tuple(UserClinicRole.Role.choices),
        "references": REFERENCE_CHOICES,
        "state": state,
        "message": message,
        "exceptions": exceptions,
        "runs": runs,
    }
    template = (
        "workflows/partials/queue.html"
        if request.headers.get("HX-Request") == "true"
        else "workflows/tasks.html"
    )
    return render(request, template, context, status=status)
