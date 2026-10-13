"""Five-step server checklist, with no provider activation authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.identity.onboarding import (
    onboarding_progress,
    provider_status,
    review_provider_status,
)
from apps.identity.otp import auth_redirect, auth_response, privileged_totp_required

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


@dataclass(frozen=True, slots=True)
class OnboardingItem:
    """An authorized real check and an existing native action."""

    key: str
    label: str
    completed: bool
    href: str | None


@require_http_methods(["GET", "POST"])
@privileged_totp_required(
    lambda clinic_id: reverse("identity:onboarding", kwargs={"clinic_id": clinic_id})
)
def onboarding_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Refresh current checks on POST; GET never persists checklist receipts."""
    progress = onboarding_progress(
        clinic_id=clinic_id, persist=request.method == "POST"
    )
    labels = {
        "organization_unit": _("Organization and clinic"),
        "staff_invitations": _("Invite your staff"),
        "recovery_enrollment": _("Save your recovery codes"),
        "provider_connections": _("Review provider status"),
        "opening_agenda": _("Open your first agenda"),
    }
    destinations = {
        "organization_unit": reverse(
            "identity:clinic-settings", kwargs={"clinic_id": clinic_id}
        ),
        "staff_invitations": reverse("identity:staff", kwargs={"clinic_id": clinic_id}),
        "recovery_enrollment": reverse(
            "identity:recovery", kwargs={"clinic_id": clinic_id}
        ),
        "opening_agenda": reverse("scheduling:agenda", kwargs={"clinic_id": clinic_id}),
    }
    items = tuple(
        OnboardingItem(
            step.key,
            labels.get(step.key, _("Additional setup step")),
            step.completed,
            destinations.get(step.key),
        )
        for step in progress
    )
    return auth_response(
        render(
            request,
            "identity/onboarding.html",
            {
                "clinic_id": clinic_id,
                "steps": items,
                "providers": provider_status(clinic_id=clinic_id),
                "refreshed": request.method == "POST",
            },
        )
    )


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:onboarding", kwargs={"clinic_id": clinic_id})
)
def provider_review(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Persist only review of the current read-only capability statuses."""
    review_provider_status(clinic_id=clinic_id)
    return auth_redirect(
        request, reverse("identity:onboarding", kwargs={"clinic_id": clinic_id})
    )
