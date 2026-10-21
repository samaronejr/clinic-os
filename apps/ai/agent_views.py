"""Automations queue and versioned policy forms with real step-up approval."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionInputError,
    ActionRefusedError,
)
from apps.ai.agent_forms import ActionDecisionForm, AgentPolicyForm
from apps.ai.agent_policies import policy_administrator, publish_agent_policy
from apps.ai.agent_presenter import present_proposal
from apps.ai.approvals import approve_action, reject_action
from apps.ai.attestations import step_up_evidence
from apps.ai.models import ActionProposal, AgentPolicyVersion
from apps.core.secrets import SecretUnavailableError
from apps.identity.current_context import CurrentActorError
from apps.identity.otp import flow_redirect, privileged_totp_required
from apps.identity.stepup import StepUpRequired

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse, HttpResponseBase


def _queue_url(clinic_id: UUID) -> str:
    return reverse("ai:agents", args=(clinic_id,))


@privileged_totp_required(_queue_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def agent_queue(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Review exact effects and retained exceptions inside the staff workspace."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.agent_staff(%s)", [clinic_id])
        if cursor.fetchone() != (True,):
            return render(request, "403.html", status=403)
    error = ""
    status = 200
    if request.method == "POST":
        form = ActionDecisionForm(request.POST)
        if (
            set(request.POST) - {*form.fields, "csrfmiddlewaretoken"}
            or request.FILES
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
            or not form.is_valid()
        ):
            return render(request, "400.html", status=400)
        try:
            _decide(request, clinic_id, form)
        except (ActionAccessDeniedError, CurrentActorError):
            return render(request, "403.html", status=403)
        except StepUpRequired:
            return flow_redirect(request, "identity:step-up", _queue_url(clinic_id))
        except (ActionRefusedError, ActionInputError):
            error = _(
                "This proposal changed or is no longer eligible. "
                "Refresh and review it again."
            )
            status = 409
        except SecretUnavailableError:
            error = _(
                "Agent approval is unavailable. "
                "Ask an administrator to check its managed key."
            )
            status = 503
        else:
            return redirect(_queue_url(clinic_id))
    proposals = (
        ActionProposal.objects.filter(clinic_id=clinic_id)
        .select_related("run__policy", "clinic")
        .order_by("-created_at")[:100]
    )
    response = render(
        request,
        "ai/automations.html",
        {
            "clinic_id": clinic_id,
            "rows": [present_proposal(row) for row in proposals],
            "error": error,
        },
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response


def _decide(request: HttpRequest, clinic_id: UUID, form: ActionDecisionForm) -> None:
    data = form.cleaned_data
    if data["command"] == "reject":
        reject_action(
            clinic_id=clinic_id,
            proposal_id=data["proposal_id"],
            expected_digest=data["digest"],
        )
        return
    approval = approve_action(
        clinic_id=clinic_id,
        proposal_id=data["proposal_id"],
        expected_digest=data["digest"],
        evidence=step_up_evidence(request, clinic_id=clinic_id, digest=data["digest"]),
    )
    from apps.ai.execution_tasks import dispatch_approved  # noqa: PLC0415

    dispatch_approved(clinic_id=clinic_id, proposal_id=approval.proposal_id)


@privileged_totp_required(_queue_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def agent_settings(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Publish default-off capability policies with registered tool schemas."""
    try:
        policy_administrator(clinic_id=clinic_id)
    except CurrentActorError:
        return render(request, "403.html", status=403)
    capability = request.POST.get("capability", "")
    if request.method == "POST" and capability not in ("front_desk", "analyst"):
        return render(request, "400.html", status=400)
    forms: list[tuple[str, str, AgentPolicyForm]] = []
    status = 200
    for key, label in (
        ("front_desk", _("Front desk drafts")),
        ("analyst", _("Operational analysis")),
    ):
        current = (
            AgentPolicyVersion.objects.filter(clinic_id=clinic_id, capability=key)
            .order_by("-version")
            .first()
        )
        initial = {
            "expected_version": current.version if current else 0,
            "enabled": current.enabled if current else False,
            "autonomy_ceiling": current.autonomy_ceiling if current else "A0",
            "tools": list(current.tool_allowlist) if current else [],
            "principal_id": current.principal_id if current else None,
            "budget_actions": current.budget_actions if current else 20,
            "budget_micros": current.budget_micros if current else 20,
            "timeout_seconds": current.timeout_seconds if current else 30,
            "compensation": current.compensation_policy["mode"]
            if current
            else "manual",
        }
        form = AgentPolicyForm(
            request.POST if capability == key else None, prefix=key, initial=initial
        )
        if capability == key:
            permitted = {f"{key}-{name}" for name in form.fields} | {
                "csrfmiddlewaretoken",
                "capability",
            }
            if (
                set(request.POST) - permitted
                or request.FILES
                or any(
                    len(request.POST.getlist(name)) != 1
                    for name in request.POST
                    if name != key + "-tools"
                )
            ):
                return render(request, "400.html", status=400)
            if form.is_valid():
                try:
                    publish_agent_policy(
                        clinic_id=clinic_id,
                        expected_version=form.cleaned_data["expected_version"],
                        terms=form.terms(key),
                    )
                except (ActionInputError, ActionRefusedError):
                    form.add_error(
                        None,
                        _(
                            "Check the autonomy, registered tools "
                            "and current policy version."
                        ),
                    )
                    status = 409
                except ActionAccessDeniedError:
                    return render(request, "403.html", status=403)
                else:
                    return redirect(reverse("ai:agent-settings", args=(clinic_id,)))
            else:
                status = 400
        forms.append((key, label, form))
    response = render(
        request,
        "ai/agent_settings.html",
        {"clinic_id": clinic_id, "forms": forms},
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response
