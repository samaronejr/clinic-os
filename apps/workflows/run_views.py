"""Native pinned-run and exception commands, never silent automatic replay."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django import forms
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.identity.current_context import CurrentActorError
from apps.identity.otp import privileged_totp_required
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    require_clinic_access,
    require_manager,
)
from apps.workflows.designer_forms import RunForm, RunSelectorForm
from apps.workflows.models import WorkflowDefinitionVersion, WorkflowRun
from apps.workflows.run_control import retry_run
from apps.workflows.run_services import cancel_run, start_run
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError
from apps.workflows.views import _manager

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase


def _location(clinic_id: UUID, *, exceptions: bool = False) -> str:
    return reverse(
        "workflows:run-exceptions" if exceptions else "workflows:runs",
        args=(clinic_id,),
    )


def runs(
    request: HttpRequest, clinic_id: UUID, *, exceptions: bool = False
) -> HttpResponseBase:
    """Decide view scope before any authenticated shell/session work."""
    try:
        require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
        return _authorized_runs(request, clinic_id, exceptions=exceptions)
    except CurrentActorError:
        return render(request, "403.html", status=403)


@privileged_totp_required(_location)
def _authorized_runs(
    request: HttpRequest, clinic_id: UUID, *, exceptions: bool = False
) -> HttpResponseBase:
    clinic, actor = require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
    if request.method not in {"GET", "POST"}:
        return render(request, "403.html", status=403)
    error = ""
    status = 200
    if request.method == "POST":
        try:
            _command(request, clinic_id)
        except WorkflowInputError:
            error, status = _("Check the workflow details."), 400
        except WorkflowConflictError:
            error, status = (
                _("This run changed or cannot be retried safely. Review its steps."),
                409,
            )
        else:
            return redirect(_location(clinic_id, exceptions=exceptions))
    queue = (
        WorkflowRun.objects.filter(clinic_id=clinic_id)
        .select_related("definition_version")
        .prefetch_related("steps")
        .order_by("-created_at")
    )
    if exceptions:
        queue = queue.filter(
            Q(state="failed")
            | Q(steps__error_code__in=("operation_unknown", "operation_failed"))
            | Q(
                state__in=("pending", "running", "waiting"),
                updated_at__lt=timezone.now() - timedelta(seconds=60),
            )
        ).distinct()
    published = tuple(
        WorkflowDefinitionVersion.objects.filter(
            clinic_id=clinic_id, state="published"
        ).order_by("key", "-version")
    )
    run_form = RunForm(
        initial={
            "subject_kind": "clinic",
            "subject_id": clinic_id,
            "idempotency_key": uuid4(),
        }
    )
    run_form.fields["definition_version_id"].widget = forms.Select(
        choices=[
            (str(version.pk), f"{version.key} / {version.version}")
            for version in published
        ]
    )
    manager = _manager(clinic_id)
    rows = []
    for run in queue[:100]:
        steps = tuple(run.steps.all())
        failed = tuple(step for step in steps if step.state == "failed")
        rows.append(
            {
                "run": run,
                "command_key": uuid4(),
                "retryable": manager
                and run.started_by_id == actor
                and run.state == "failed"
                and bool(failed)
                and not any(step.state == "cancelled" for step in steps)
                and all(
                    step.operation_id is None and step.created_task_id is None
                    for step in failed
                ),
                "steps": tuple(
                    {
                        "record": step,
                        "handler": run.definition_version.steps[step.position][
                            "handler"
                        ],
                    }
                    for step in steps
                ),
            }
        )
    return render(
        request,
        "workflows/runs.html",
        {
            "clinic": clinic,
            "runs": tuple(rows),
            "published": published,
            "run_form": run_form,
            "manager": manager,
            "exceptions": exceptions,
            "workflow_error": error,
        },
        status=status,
    )


def _command(request: HttpRequest, clinic_id: UUID) -> None:
    require_manager(clinic_id=clinic_id)
    action = request.POST.get("action")
    if action == "start":
        form = RunForm(request.POST)
        if not form.is_valid():
            raise WorkflowInputError
        values = form.cleaned_data
        start_run(
            clinic_id=clinic_id,
            definition_version_id=values["definition_version_id"],
            context_refs=[
                {
                    "kind": values["subject_kind"],
                    "id": str(values["subject_id"]),
                }
            ],
            idempotency_key=values["idempotency_key"],
        )
        return
    selector = RunSelectorForm(request.POST)
    if not selector.is_valid():
        raise WorkflowAccessDeniedError
    run_id = selector.cleaned_data["run_id"]
    revision = selector.cleaned_data["expected_revision"]
    match action:
        case "cancel":
            current = (
                WorkflowRun.objects.select_for_update()
                .filter(clinic_id=clinic_id, pk=run_id)
                .first()
            )
            if current is None:
                raise WorkflowAccessDeniedError
            if current.revision != revision:
                raise WorkflowConflictError
            cancel_run(clinic_id=clinic_id, run_id=run_id)
        case "retry":
            command_key = selector.cleaned_data["idempotency_key"]
            if command_key is None:
                raise WorkflowInputError
            retry_run(
                clinic_id=clinic_id,
                run_id=run_id,
                expected_revision=revision,
                idempotency_key=command_key,
            )
        case _:
            raise WorkflowAccessDeniedError
