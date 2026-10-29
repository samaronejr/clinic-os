"""Native task command parsing; domain services own every write decision."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.identity.current_context import require_permission
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    require_manager,
    require_task_access,
)
from apps.workflows.forms import CommentSelectorForm, CreateTaskForm, TaskSelectorForm
from apps.workflows.reassignment import (
    ReassignmentPreview,
    apply_reassignment,
    preview_reassignment,
)
from apps.workflows.services import (
    TaskOwner,
    TaskSpec,
    WorkflowInputError,
    add_comment,
    assign_task,
    cancel_task,
    complete_task,
    create_task,
    start_task,
)

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.identity.models import Clinic

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


def _assignment_owner(
    request: HttpRequest, clinic_id: UUID, task_id: UUID, actor: UUID
) -> TaskOwner:
    # Authority before input: an actor who may not assign this task gets the
    # identical denial, never a validation response about the owner field.
    require_task_access(clinic_id=clinic_id, task_id=task_id, permission="tasks.assign")
    return _owner(request.POST.get("owner", "me"), actor)


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
            owner=_assignment_owner(request, clinic_id, task_id, actor),
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
