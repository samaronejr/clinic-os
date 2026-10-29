"""Versioned native designer; every transition is a domain service command."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.workflows.access import (
    WorkflowAccessDeniedError,
    require_clinic_access,
    require_manager,
)
from apps.workflows.definition_services import (
    create_definition,
    publish_definition_draft,
    retire_definition,
    revise_definition,
    validate_definition,
)
from apps.workflows.designer_forms import DefinitionForm, StepForm, VersionSelectorForm
from apps.workflows.models import WorkflowDefinitionVersion
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase


def _location(clinic_id: UUID) -> str:
    return reverse("workflows:designer", args=(clinic_id,))


def designer(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Authorize before OTP/session work, including malformed commands."""
    try:
        require_manager(clinic_id=clinic_id)
        return _authorized_designer(request, clinic_id)
    except CurrentActorError:
        return render(request, "403.html", status=403)


@privileged_totp_required(_location)
def _authorized_designer(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    require_manager(clinic_id=clinic_id)
    clinic, _actor = require_clinic_access(clinic_id=clinic_id, permission="tasks.view")
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
            error, status = _("This version changed. Review it before continuing."), 409
        else:
            return redirect(_location(clinic_id))
    definitions = WorkflowDefinitionVersion.objects.filter(
        clinic_id=clinic_id
    ).order_by("key", "-version")[:100]
    step_initial = {
        "handler": "task",
        "task_kind": "checklist",
        "owner_role": "receptionist",
        "subject": 0,
        "seconds": 60,
        "offset": 3600,
    }
    return render(
        request,
        "workflows/designer.html",
        {
            "clinic": clinic,
            "definitions": tuple(
                {
                    "definition": definition,
                    "state_label": _(
                        WorkflowDefinitionVersion.State(definition.state).label
                    ),
                    "steps": tuple(
                        {
                            **step,
                            "owner_label": UserClinicRole.Role(step["owner_role"]).label
                            if step["handler"] == "task"
                            else "",
                        }
                        for step in definition.steps
                    ),
                    "command_key": uuid4(),
                    "step_form": StepForm(
                        prefix=str(definition.pk), initial=step_initial
                    ),
                }
                for definition in definitions
            ),
            "definition_form": DefinitionForm(initial={"idempotency_key": uuid4()}),
            "workflow_error": error,
        },
        status=status,
    )


def _command(request: HttpRequest, clinic_id: UUID) -> None:
    action = request.POST.get("action")
    if action == "create":
        form = DefinitionForm(request.POST)
        if not form.is_valid():
            raise WorkflowInputError
        create_definition(
            clinic_id=clinic_id,
            key=form.cleaned_data["key"],
            steps=form.steps(),
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
        return
    selector = VersionSelectorForm(request.POST)
    if (
        not selector.is_valid()
        or (
            definition := WorkflowDefinitionVersion.objects.filter(
                clinic_id=clinic_id, pk=selector.cleaned_data["definition_id"]
            ).first()
        )
        is None
    ):
        raise WorkflowAccessDeniedError
    identifier = selector.cleaned_data["definition_id"]
    revision = selector.cleaned_data["expected_revision"]
    command_key = selector.cleaned_data["idempotency_key"]
    match action:
        case "add-step":
            step_form = StepForm(request.POST, prefix=str(identifier))
            if not step_form.is_valid():
                raise WorkflowInputError
            revise_definition(
                clinic_id=clinic_id,
                definition_version_id=identifier,
                steps=[*definition.steps, step_form.step()],
                expected_revision=revision,
                idempotency_key=command_key,
            )
        case "remove-step":
            revise_definition(
                clinic_id=clinic_id,
                definition_version_id=identifier,
                steps=definition.steps[:-1],
                expected_revision=revision,
                idempotency_key=command_key,
            )
        case "validate":
            validate_definition(
                clinic_id=clinic_id,
                definition_version_id=identifier,
                expected_revision=revision,
                idempotency_key=command_key,
            )
        case "publish":
            publish_definition_draft(
                clinic_id=clinic_id,
                definition_version_id=identifier,
                expected_revision=revision,
                idempotency_key=command_key,
            )
        case "retire":
            retire_definition(
                clinic_id=clinic_id,
                definition_version_id=identifier,
                expected_revision=revision,
                idempotency_key=command_key,
            )
        case _:
            raise WorkflowAccessDeniedError
