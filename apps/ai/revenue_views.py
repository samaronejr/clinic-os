"""Private native finance review; every record selector stays in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionInputError,
    ActionRefusedError,
    Refusal,
)
from apps.ai.action_payload import closed, identifier, parse_payload
from apps.ai.agent_presenter import present_proposal
from apps.ai.approvals import approve_action, reject_action
from apps.ai.attestations import step_up_evidence
from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
)
from apps.ai.execution import execute_action
from apps.ai.models import ActionProposal, AIArtifact
from apps.ai.revenue_cycle import (
    RevenueReview,
    collect_revenue_cycle,
    propose_revenue_action,
    request_revenue_cycle,
)
from apps.ai.revenue_sources import require_finance
from apps.core.secrets import SecretUnavailableError
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.otp import flow_redirect, privileged_totp_required
from apps.identity.stepup import StepUpRequired
from apps.insurance.access import authority
from apps.insurance.errors import InsuranceDeniedError
from apps.insurance.models import Guide

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


class RevenueForm(forms.Form):
    """Parse a closed command; authorization never comes from the submitted ids."""

    command = forms.ChoiceField(
        choices=[
            (key, key) for key in ("request", "refresh", "propose", "approve", "reject")
        ]
    )
    guide_id = forms.UUIDField(required=False)
    invocation_id = forms.UUIDField(required=False)
    output_id = forms.UUIDField(required=False)
    position = forms.IntegerField(min_value=0, max_value=191, required=False)
    proposal_id = forms.UUIDField(required=False)
    digest = forms.RegexField(r"^[0-9a-f]{64}$", required=False)

    def clean(self) -> dict[str, object]:
        """Exactly the fields needed for this command must have parsed values."""
        super().clean()
        data = self.cleaned_data
        required = {
            "request": {"guide_id"},
            "refresh": {"invocation_id"},
            "propose": {"output_id", "position"},
            "approve": {"proposal_id", "digest"},
            "reject": {"proposal_id", "digest"},
        }
        expected = required.get(str(data.get("command")), set())
        if set(self.data) != {"command", "csrfmiddlewaretoken", *expected} or any(
            data.get(key) is None or data.get(key) == "" for key in expected
        ):
            raise forms.ValidationError(
                _("Choose the source and review the current proposal.")
            )
        return data


def _url(clinic_id: UUID) -> str:
    return reverse("ai:revenue-cycle", args=(clinic_id,))


def _can_approve(clinic_id: UUID) -> bool:
    try:
        authority(clinic_id=clinic_id, permission="automation.finance")
    except InsuranceDeniedError:
        return False
    return True


def _review_for_proposal(clinic_id: UUID, proposal: ActionProposal) -> RevenueReview:
    data = parse_payload(proposal.payload).data()
    if proposal.run.policy.capability != "revenue_cycle":
        raise GatewayAccessDeniedError
    subject = closed(data["subject"], {"kind", "id", "expected_state", "revision"})
    output = AIArtifact.objects.filter(
        pk=identifier(subject["id"]),
        clinic_id=clinic_id,
        kind="output",
    ).first()
    if output is None or output.invocation_id is None:
        raise GatewayAccessDeniedError
    return collect_revenue_cycle(
        clinic_id=clinic_id, invocation_id=output.invocation_id
    )


def _command(
    request: HttpRequest, clinic_id: UUID, form: RevenueForm
) -> tuple[RevenueReview, ActionProposal | None, str]:
    data = form.cleaned_data
    if data["command"] == "request":
        return (
            request_revenue_cycle(clinic_id=clinic_id, guide_id=data["guide_id"]),
            None,
            "",
        )
    if data["command"] == "refresh":
        return (
            collect_revenue_cycle(
                clinic_id=clinic_id, invocation_id=data["invocation_id"]
            ),
            None,
            "",
        )
    if data["command"] == "propose":
        proposal = propose_revenue_action(
            clinic_id=clinic_id, output_id=data["output_id"], position=data["position"]
        )
        return _review_for_proposal(clinic_id, proposal), proposal, ""
    require_permission("automation.finance", clinic_id=clinic_id)
    selected = (
        ActionProposal.objects.select_related("run__policy", "clinic")
        .filter(
            pk=data["proposal_id"],
            clinic_id=clinic_id,
            run__policy__capability="revenue_cycle",
        )
        .first()
    )
    if selected is None:
        raise GatewayAccessDeniedError
    proposal = selected
    if data["command"] == "reject":
        reject_action(
            clinic_id=clinic_id, proposal_id=proposal.pk, expected_digest=data["digest"]
        )
        notice = _("Proposal rejected. No claim or billing action was performed.")
    else:
        approve_action(
            clinic_id=clinic_id,
            proposal_id=proposal.pk,
            expected_digest=data["digest"],
            evidence=step_up_evidence(
                request, clinic_id=clinic_id, digest=data["digest"]
            ),
        )
        outcome = execute_action(clinic_id=clinic_id, proposal_id=proposal.pk)
        if outcome.state != "receipt":
            raise ActionRefusedError(outcome.reason or Refusal.STATE)
        notice = _(
            "Review recorded. An appeal remains a draft and needs separate "
            "human submission approval."
        )
    proposal.refresh_from_db()
    return _review_for_proposal(clinic_id, proposal), proposal, notice


@privileged_totp_required(_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def revenue_cycle_page(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Finance sees source-backed suggestions; approval also needs queue authority."""
    try:
        require_finance(clinic_id=clinic_id)
    except GatewayAccessDeniedError:
        return render(request, "403.html", status=403)
    if (
        request.GET
        or request.FILES
        or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
    ):
        return render(request, "400.html", status=400)
    review = None
    proposal = None
    error = ""
    notice = ""
    status = 200
    if request.method == "POST":
        form = RevenueForm(request.POST)
        if not form.is_valid():
            error, status = _("Choose the source and review the current proposal."), 400
        else:
            try:
                review, proposal, notice = _command(request, clinic_id, form)
            except (
                GatewayAccessDeniedError,
                ActionAccessDeniedError,
                CurrentActorError,
            ):
                return render(request, "403.html", status=403)
            except StepUpRequired:
                return flow_redirect(request, "identity:step-up", _url(clinic_id))
            except (ActionRefusedError, ActionInputError, GatewayInputError):
                error, status = (
                    _("The source or proposal changed. Refresh and review it again."),
                    409,
                )
            except (GatewayRefusedError, SecretUnavailableError):
                error, status = (
                    _(
                        "The assistant is unavailable. "
                        "Continue the financial workflow manually."
                    ),
                    503,
                )
    response = render(
        request,
        "ai/revenue_cycle.html",
        {
            "clinic_id": clinic_id,
            "review": review,
            "proposal": present_proposal(proposal) if proposal else None,
            "can_approve": _can_approve(clinic_id),
            "error": error,
            "notice": notice,
            "guides": Guide.objects.filter(clinic_id=clinic_id).order_by(
                "-created_at", "-id"
            )[:100],
        },
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response
