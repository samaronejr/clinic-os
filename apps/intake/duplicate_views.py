"""Native human duplicate review, unified timelines and explicit reversal."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import bind_patient_context
from apps.ehr.patient_timeline import patient_record_timeline
from apps.identity.otp import flow_redirect, privileged_totp_required
from apps.identity.stepup import StepUpRequired
from apps.intake.access import PatientAccessDeniedError
from apps.intake.alias_access import (
    DuplicateConflictError,
    DuplicateInputError,
    PatientAliasedError,
    duplicate_review_session,
    review_actor,
)
from apps.intake.demographics import (
    DemographicsInputError,
    DemographicsNameRequiredError,
    DemographicsRequiredError,
    DemographicsStaleError,
    demographics_profile,
)
from apps.intake.duplicate_forms import DuplicateReviewForm, DuplicateSelectionForm
from apps.intake.duplicates import adjudicate_duplicate, unmerge
from apps.intake.forms import EnrollmentForm
from apps.intake.merge_review import merge_review
from apps.intake.models import DuplicateCandidate
from apps.tenancy.middleware import HANDLED_REJECTION_ATTR

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase


def continuation(clinic_id: UUID) -> str:
    """Resume on a safe GET without record selectors."""
    return reverse("intake:patient-duplicates", args=(clinic_id,))


def _review(
    request: HttpRequest, clinic_id: UUID, candidate: DuplicateCandidate
) -> dict[str, object]:
    first, second = (
        demographics_profile(clinic_id=clinic_id, enrollment_id=enrollment_id)
        for enrollment_id in (candidate.enrollment_a_id, candidate.enrollment_b_id)
    )
    form = DuplicateReviewForm(
        candidate=candidate,
        first=first,
        second=second,
        data=request.POST if request.POST.get("action") == "merge" else None,
    )
    return {"candidate": candidate, "form": form, "first": first, "second": second}


def _decision_action(
    request: HttpRequest, clinic_id: UUID, selection: DuplicateSelectionForm
) -> dict[str, object]:
    """Review an affected clinic or reverse at the decision's authorized origin."""
    decision_id = selection.cleaned_data["merge_decision_id"]
    if decision_id is None:
        raise Http404
    if request.POST["action"] == "unmerge":
        with duplicate_review_session(
            clinic_id=clinic_id, session_key=request.session.session_key or ""
        ):
            unmerge(
                clinic_id=clinic_id,
                merge_decision_id=decision_id,
                reason=selection.cleaned_data["reason"],
            )
        return {
            "review": merge_review(clinic_id=clinic_id, merge_decision_id=decision_id),
            "notice": _(
                "Registrations separated. Later survivor records remain there "
                "and require review."
            ),
        }
    return {"review": merge_review(clinic_id=clinic_id, merge_decision_id=decision_id)}


def _action(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    selection = DuplicateSelectionForm(request.POST, auto_id="unmerge-%s")
    if not selection.is_valid():
        decision_id = selection.cleaned_data.get("merge_decision_id")
        if (
            request.POST.get("action") == "unmerge"
            and isinstance(decision_id, UUID)
            and set(selection.errors) == {"reason"}
        ):
            return {
                "review": merge_review(
                    clinic_id=clinic_id, merge_decision_id=decision_id
                ),
                "unmerge_form": selection,
                "rejected": True,
            }
        raise Http404
    action = request.POST.get("action", "")
    if action in {"review_decision", "unmerge"}:
        return _decision_action(request, clinic_id, selection)
    candidate = DuplicateCandidate.objects.filter(
        pk=selection.cleaned_data["candidate_id"], clinic_id=clinic_id
    ).first()
    if candidate is None:
        raise PatientAccessDeniedError
    if action == "review":
        if candidate.state == DuplicateCandidate.State.OPEN:
            return _review(request, clinic_id, candidate)
        return {
            "review": merge_review(
                clinic_id=clinic_id, merge_decision_id=candidate.mergedecision.pk
            )
        }
    if action not in {"merge", "dismiss"}:
        raise Http404
    context = _review(request, clinic_id, candidate)
    form = context["form"]
    if not isinstance(form, DuplicateReviewForm):
        raise TypeError
    if action == "merge" and not form.is_valid():
        context["rejected"] = True
        return context
    survivor = (
        UUID(form.cleaned_data["survivor_patient_id"])
        if action == "merge"
        else candidate.enrollment_a.patient_id
    )
    with duplicate_review_session(
        clinic_id=clinic_id, session_key=request.session.session_key or ""
    ):
        decision = adjudicate_duplicate(
            clinic_id=clinic_id,
            candidate_id=candidate.pk,
            decision=action,
            survivor_patient_id=survivor,
            field_choices=form.field_choices() if action == "merge" else {},
            reason=selection.cleaned_data["reason"],
        )
    return {
        "review": merge_review(clinic_id=clinic_id, merge_decision_id=decision.pk),
        "issued_invitations": decision.issued_invitations,
        "notice": _(
            "Decision recorded. Historical records keep their original registration."
        ),
    }


@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_duplicates(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Review deterministic hints; only verified explicit decisions change lineage."""
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "unmerge_form": DuplicateSelectionForm(auto_id="unmerge-%s"),
    }
    try:
        review_actor(clinic_id)
        if request.method == "POST":
            context.update(_action(request, clinic_id))
    except PatientAccessDeniedError as error:
        raise Http404 from error
    except StepUpRequired:
        return flow_redirect(request, "identity:step-up", continuation(clinic_id))
    except (DuplicateConflictError, DemographicsStaleError, PatientAliasedError):
        context.update(
            {
                "error": _(
                    "This pair changed. Open the current registrations before deciding."
                ),
                "rejected": True,
            }
        )
    except (
        DuplicateInputError,
        DemographicsInputError,
        DemographicsNameRequiredError,
        DemographicsRequiredError,
    ):
        context.update(
            {
                "error": _("Enter a reason and review each demographic choice."),
                "rejected": True,
            }
        )
    context["candidates"] = (
        DuplicateCandidate.objects.filter(clinic_id=clinic_id)
        .select_related("enrollment_a__patient", "enrollment_b__patient")
        .order_by("-created_at", "id")[:100]
    )
    response = render(request, "intake/patient_duplicates.html", context)
    if context.get("rejected"):
        setattr(response, HANDLED_REJECTION_ATTR, True)
    return response


@require_http_methods(["POST"])
def patient_timeline(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Render same-clinic finalized lineage through existing clinical authority."""
    selection = EnrollmentForm(request.POST)
    if not selection.is_valid():
        raise Http404
    try:
        timeline = patient_record_timeline(
            clinic_id=clinic_id, enrollment_id=selection.selected_enrollment()
        )
    except PatientAccessDeniedError as error:
        raise Http404 from error
    bind_patient_context(
        request, clinic_id=clinic_id, enrollment_id=selection.selected_enrollment()
    )
    return render(request, "intake/patient_timeline.html", {"timeline": timeline})
