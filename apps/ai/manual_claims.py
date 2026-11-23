"""The human fallback for claim denials; deliberately no model or action queue."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django import forms
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.insurance.access import authority, scoped
from apps.insurance.errors import (
    InsuranceConflictError,
    InsuranceDeniedError,
    InsuranceInputError,
)
from apps.insurance.limits import MAX_APPEAL_TEXT
from apps.insurance.models import Glosa, Guide
from apps.insurance.revenue import draft_appeal

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


class InspectClaim(forms.Form):
    """One body-only selector. No posted amounts or payer facts."""

    guide_id = forms.UUIDField()


class ManualAppeal(forms.Form):
    """A human-authored draft. Submission, coding and money are not commands."""

    glosa_id = forms.UUIDField()
    command_id = forms.UUIDField()
    body = forms.CharField(max_length=MAX_APPEAL_TEXT)


def _url(clinic_id: UUID) -> str:
    return reverse("ai:manual-claims", args=(clinic_id,))


def _command(request: HttpRequest, clinic_id: UUID, context: dict[str, object]) -> UUID:
    command = request.POST.get("command")
    form: forms.Form
    if command == "inspect":
        form = InspectClaim(request.POST)
    elif command == "draft":
        form = ManualAppeal(request.POST)
    else:
        raise InsuranceInputError
    if (
        set(request.POST) != {"csrfmiddlewaretoken", "command", *form.fields}
        or not form.is_valid()
    ):
        raise InsuranceInputError
    if command == "draft":
        appeal = draft_appeal(
            clinic_id=clinic_id, clinical_assertions="", **form.cleaned_data
        )
        context["saved"] = appeal
        return appeal.glosa.guide_id
    guide_id: UUID = form.cleaned_data["guide_id"]
    return guide_id


@privileged_totp_required(_url)
@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def manual_claims(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Inspect actual payer denials, then save only an unsubmitted appeal draft."""
    try:
        for permission in ("tiss.read", "tiss.manage"):
            authority(clinic_id=clinic_id, permission=permission)
        if (
            request.GET
            or request.FILES
            or any(len(request.POST.getlist(k)) != 1 for k in request.POST)
        ):
            return render(request, "400.html", status=400)
        context: dict[str, object] = {
            "clinic_id": clinic_id,
            "maximum": MAX_APPEAL_TEXT,
        }
        if request.method == "POST":
            guide_id = _command(request, clinic_id, context)
            context["selected"] = scoped(
                model=Guide, clinic_id=clinic_id, identifier=guide_id
            )
            context["denials"] = [
                (row, uuid4())
                for row in Glosa.objects.filter(
                    clinic_id=clinic_id, guide_id=guide_id, denied_minor__gt=0
                )
                .only(
                    "sequence",
                    "charged_minor",
                    "accepted_minor",
                    "denied_minor",
                    "code",
                )
                .order_by("sequence", "pk")
            ]
        context["guides"] = (
            Guide.objects.filter(
                clinic_id=clinic_id,
                pk__in=Glosa.objects.filter(
                    clinic_id=clinic_id, denied_minor__gt=0
                ).values("guide_id"),
            )
            .only("number")
            .order_by("-created_at", "pk")
        )
        return render(request, "ai/manual_claims.html", context)
    except InsuranceDeniedError:
        return render(request, "403.html", status=403)
    except InsuranceInputError:
        return render(request, "400.html", status=400)
    except InsuranceConflictError:
        return render(
            request,
            "ai/manual_claims.html",
            {"clinic_id": clinic_id, "conflict": True},
            status=409,
        )
