"""Blinded native adjudication; selectors remain in POST and server-side sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.evaluation.forms import LABELS, RatingForm
from apps.ai.evaluation.protocol import ERROR_LABELS, EvaluationInputError, category
from apps.ai.evaluation.services import (
    authorized_run,
    decide_change,
    evaluation_staff,
    rate_case,
    review_slot,
)
from apps.ai.models import EvaluationRating, EvaluationRun, ModelChangeRequest
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from django.http import HttpRequest


def _location(clinic_id: UUID) -> str:
    return reverse("ai:evaluation", args=(clinic_id,))


@privileged_totp_required(_location)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def evaluation_view(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Render each translated refusal after its command savepoint has rolled back."""
    try:
        response = _workspace(request, clinic_id)
    except (CurrentActorError, GatewayAccessDeniedError):
        response = render(request, "403.html", status=403)
    except (EvaluationInputError, ValueError, ValidationError, DatabaseError):
        response = render(
            request,
            "ai/evaluation.html",
            {
                "clinic_id": clinic_id,
                "error": _(
                    "Evaluation changed or review is incomplete. "
                    "Refresh and review again."
                ),
            },
            status=409,
        )
    response["Cache-Control"] = "private, no-store"
    return response


@transaction.atomic
def _workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    actor = current_actor_id()
    try:
        evaluation_staff(clinic_id=clinic_id, mode="clinician")
    except GatewayAccessDeniedError:
        evaluation_staff(clinic_id=clinic_id, mode="owner")
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "runs": EvaluationRun.objects.filter(
            Q(rater_one_id=actor) | Q(rater_two_id=actor) | Q(adjudicator_id=actor),
            clinic_id=clinic_id,
        ).order_by("-created_at")[:50],
        "changes": ModelChangeRequest.objects.filter(
            clinic_id=clinic_id, status="requested", run__status="passed"
        )
        .exclude(requested_by_id=actor)
        .select_related("run__protocol")
        .order_by("created_at")[:50],
    }
    if request.method == "GET":
        return render(request, "ai/evaluation.html", context)
    permitted = {
        "csrfmiddlewaretoken",
        "action",
        "run_id",
        "case_id",
        "change_id",
        "approval_ref",
        "correction_ms",
        *ERROR_LABELS,
    }
    if (
        request.FILES
        or set(request.POST) - permitted
        or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
    ):
        return HttpResponse(status=400)
    action = request.POST.get("action")
    if action in {"approved", "rejected"}:
        decide_change(
            clinic_id=clinic_id,
            change_id=UUID(request.POST.get("change_id", "")),
            decision="approved" if action == "approved" else "rejected",
            approval_ref=request.POST.get("approval_ref", ""),
        )
        context["saved"] = True
        return render(request, "ai/evaluation.html", context)
    return _review(request, clinic_id, context)


def _review(
    request: HttpRequest, clinic_id: UUID, context: dict[str, object]
) -> HttpResponse:
    """Keep the blind review projection separate from owner change commands."""
    actor = current_actor_id()
    action = request.POST.get("action")
    run = authorized_run(
        clinic_id=clinic_id, run_id=UUID(request.POST.get("run_id", ""))
    )
    slot = review_slot(run, actor)
    if not isinstance(run.sample_results, dict):
        context["pending"] = True
        return render(request, "ai/evaluation.html", context)
    cases = cast("list[dict[str, object]]", run.sample_results["cases"])
    selected = request.POST.get("case_id") or str(cases[0]["id"])
    sample = next((case for case in cases if case["id"] == selected), None)
    if sample is None:
        raise EvaluationInputError
    form = RatingForm(initial={"run_id": run.pk, "case_id": selected})
    status = 200
    if action == "rate":
        form = RatingForm(request.POST)
        if form.is_valid():
            rate_case(
                clinic_id=clinic_id,
                run_id=run.pk,
                case_id=selected,
                labels={
                    name: form.cleaned_data[name]
                    for name in (*ERROR_LABELS, "correction_ms")
                },
            )
            context["saved"] = True
        else:
            status = 400
    elif action != "open":
        return HttpResponse(status=400)
    votes = {
        row.slot: row.labels
        for row in EvaluationRating.objects.filter(run=run, case_id=selected)
    }
    eligible = run.status == "pending" and slot not in votes
    if slot == "adjudicator":
        eligible = (
            eligible
            and "rater_one" in votes
            and "rater_two" in votes
            and category(votes["rater_one"]) != category(votes["rater_two"])
        )
        context["other_votes"] = [
            {
                "slot": name,
                "labels": [
                    (LABELS[label], labels[label])
                    for label in (*ERROR_LABELS, "correction_ms")
                ],
            }
            for name, labels in votes.items()
            if name in {"rater_one", "rater_two"}
        ]
    context.update(
        sample=sample,
        case_choices=[
            {"id": case["id"], "number": number} for number, case in enumerate(cases, 1)
        ],
        run_id=run.pk,
        form=form,
        eligible=eligible,
        completed=run.status != "pending",
    )
    request.session[f"ai.evaluation.{clinic_id}"] = {
        "run": str(run.pk),
        "case": selected,
    }
    return render(request, "ai/evaluation.html", context, status=status)
