"""Operations tasks: native forms with HTMX enhancement and inert refusals."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    owner_matches,
    require_clinic_access,
    require_manager,
)
from apps.workflows.forms import (
    KIND_CHOICES,
    PRIORITY_CHOICES,
    REFERENCE_CHOICES,
    STATE_CHOICES,
    CommentSelectorForm,
    CreateTaskForm,
    TaskFilterForm,
    TaskSelectorForm,
)
from apps.workflows.models import Task, WorkflowRun
from apps.workflows.reassignment import (
    ReassignmentPreview,
    apply_reassignment,
    preview_reassignment,
)
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    WorkflowConflictError,
    WorkflowInputError,
    add_comment,
    assign_task,
    cancel_task,
    complete_task,
    create_task,
    list_tasks,
    start_task,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.identity.models import Clinic


def _denied() -> HttpResponse:
    # No request context means the shell cannot select/persist a clinic or pin.
    return HttpResponse(render_to_string("403.html"), status=403)


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


def _owner(value: str, actor: UUID) -> TaskOwner:
    if value == "me":
        return TaskOwner(user_id=actor)
    kind, separator, identifier = value.partition(":")
    if not separator:
        raise WorkflowInputError
    if kind == "role":
        return TaskOwner(role=identifier)
    if kind == "user":
        try:
            return TaskOwner(user_id=UUID(identifier))
        except ValueError as error:
            raise WorkflowInputError from error
    raise WorkflowInputError


def _task_row(task: Task, staff: Mapping[UUID, str]) -> dict[str, object]:
    roles = {value: _(label) for value, label in UserClinicRole.Role.choices}
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
    }


# Closed action vocabulary. Each service below decides its own permission and
# ownership; the view adds no second, unobservable copy of those decisions.
ACTIONS = frozenset(
    {
        "filter",
        "assign",
        "start",
        "complete",
        "cancel",
        "comment",
        "bulk-preview",
        "bulk-apply",
    }
)


def _bulk_action(
    request: HttpRequest, clinic_id: UUID, actor: UUID
) -> ReassignmentPreview | None:
    require_manager(clinic_id=clinic_id)
    if request.POST["action"] == "bulk-apply":
        apply_reassignment(
            clinic_id=clinic_id, preview_token=request.POST.get("preview_token", "")
        )
        return None
    try:
        identifiers = [UUID(value) for value in request.POST.getlist("task_ids")]
    except ValueError as error:
        raise WorkflowAccessDeniedError from error
    return preview_reassignment(
        clinic_id=clinic_id,
        task_ids=identifiers,
        owner=_owner(request.POST.get("owner", ""), actor),
    )


def _submitted_evidence(request: HttpRequest) -> dict[str, object]:
    if request.POST.get("evidence_kind") == "reference":
        return {
            "record": {
                "kind": request.POST.get("record_kind", ""),
                "id": request.POST.get("record_id", ""),
            },
            "outcome": request.POST.get("outcome", ""),
        }
    return {"checked": request.POST.get("checked") == "on"}


def _action(
    request: HttpRequest, clinic_id: UUID, actor: UUID
) -> ReassignmentPreview | None:
    action = request.POST.get("action", "")
    if action not in ACTIONS:
        raise WorkflowAccessDeniedError
    if action == "filter":
        return None
    if action in {"bulk-preview", "bulk-apply"}:
        return _bulk_action(request, clinic_id, actor)
    selector = (CommentSelectorForm if action == "comment" else TaskSelectorForm)(
        request.POST
    )
    if not selector.is_valid():
        raise WorkflowAccessDeniedError
    task_id = selector.cleaned_data["task_id"]
    revision = selector.cleaned_data["expected_revision"]
    if action == "assign":
        assign_task(
            clinic_id=clinic_id,
            task_id=task_id,
            owner=_owner(request.POST.get("owner", "me"), actor),
            expected_revision=revision,
        )
    elif action == "start":
        start_task(clinic_id=clinic_id, task_id=task_id, expected_revision=revision)
    elif action == "complete":
        complete_task(
            clinic_id=clinic_id,
            task_id=task_id,
            evidence=_submitted_evidence(request),
            expected_revision=revision,
        )
    elif action == "cancel":
        cancel_task(clinic_id=clinic_id, task_id=task_id, expected_revision=revision)
    elif action == "comment":
        add_comment(
            clinic_id=clinic_id,
            task_id=task_id,
            body=request.POST.get("body", ""),
            idempotency_key=selector.cleaned_data["idempotency_key"],
        )
    return None


def _submit(
    request: HttpRequest, clinic: Clinic, actor: UUID
) -> tuple[CreateTaskForm, ReassignmentPreview | None, str, int, str]:
    form = CreateTaskForm(initial={"subject_id": clinic.pk})
    preview = None
    action = request.POST.get("action")
    if action == "create":
        require_permission("tasks.assign", clinic_id=clinic.pk)
        form = CreateTaskForm(request.POST)
        with timezone.override(ZoneInfo(str(clinic.timezone))):
            valid = form.is_valid()
        if not valid:
            return (
                form,
                None,
                "error",
                HTTPStatus.BAD_REQUEST,
                _("Review the highlighted fields."),
            )
        values = form.cleaned_data
        create_task(
            clinic_id=clinic.pk,
            spec=TaskSpec(
                kind=values["kind"],
                subject_ref={
                    "kind": values["subject_kind"],
                    "id": str(values["subject_id"]),
                },
                due_at=values["due_at"],
                priority=values["priority"],
                depends_on=values["depends_on"],
            ),
            idempotency_key=values["idempotency_key"],
        )
        form = CreateTaskForm(initial={"subject_id": clinic.pk})
    else:
        preview = _action(request, clinic.pk, actor)
    if action in {"filter", "bulk-preview"}:
        return form, preview, "default", HTTPStatus.OK, ""
    return form, preview, "success", HTTPStatus.OK, _("Task changes saved.")


def _preview_label(
    preview: ReassignmentPreview | None, staff: Mapping[UUID, str]
) -> str | None:
    if preview is None:
        return ""
    if preview.owner.user_id is not None:
        return staff.get(preview.owner.user_id)
    return _(UserClinicRole.Role(preview.owner.role).label)


def tasks(
    request: HttpRequest, clinic_id: UUID, *, exceptions: bool = False
) -> HttpResponseBase:
    """Check clinic authority before auth decorators or any shell/session work."""
    try:
        require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
    except CurrentActorError:
        return _denied()
    return _authorized_tasks(request, clinic_id, exceptions=exceptions)


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
        return _denied()
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
            return _denied()
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
    if preview_owner_label is None:
        return _denied()
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
        "roles": tuple(
            (value, _(label)) for value, label in UserClinicRole.Role.choices
        ),
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
