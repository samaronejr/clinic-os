"""Organization AI controls and a content-free degraded-state display."""

from __future__ import annotations

from typing import TYPE_CHECKING

from config.settings.contracts import SYNTHETIC_DATA_MODE
from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    ProviderUnavailableError,
    Purpose,
)
from apps.ai.controls import current_policy, set_organization_policy
from apps.ai.forms import PolicyForm
from apps.ai.models import AICircuit, GatewaySwitch
from apps.ai.policy import select_configuration
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.current_context import CurrentActorError
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse


def _continuation(clinic_id: UUID) -> str:
    return reverse("ai:settings", args=(clinic_id,))


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def settings_view(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Opt-in changes no model terms and never starts an inference request."""
    try:
        return _settings_response(request, clinic_id)
    except (CurrentActorError, GatewayAccessDeniedError):
        return render(request, "403.html", status=403)


def _settings_response(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    policy = current_policy(clinic_id=clinic_id)
    current = latest_configuration(clinic_id)
    initial = {
        "expected_version": current.version if current else 1,
        "enabled": policy.enabled,
        "purposes": policy.purposes,
        "monthly_budget_micros": policy.monthly_budget_micros,
        **{"limit_" + name: cap for name, cap in policy.purpose_budgets},
    }
    form = PolicyForm(
        request.POST if request.method == "POST" else None, initial=initial
    )
    status = 200
    if request.method == "POST":
        if (
            set(request.POST) - {*form.fields, "csrfmiddlewaretoken"}
            or request.FILES
            or any(
                len(request.POST.getlist(key)) != 1
                for key in request.POST
                if key != "purposes"
            )
        ):
            return render(request, "400.html", status=400)
        if form.is_valid():
            try:
                set_organization_policy(
                    clinic_id=clinic_id,
                    expected_version=form.cleaned_data["expected_version"],
                    policy=form.policy(),
                )
            except GatewayInputError:
                form.add_error(
                    None, _("The configuration changed. Reload before saving.")
                )
                status = 409
            else:
                return redirect(_continuation(clinic_id))
        else:
            status = 400
    global_enabled = (
        settings.CLINIC_DATA_MODE == SYNTHETIC_DATA_MODE
        and GatewaySwitch.objects.filter(pk=1, enabled=True).exists()
    )
    available = False
    if global_enabled and policy.enabled:
        for purpose in policy.purposes:
            try:
                select_configuration(clinic_id=clinic_id, purpose=Purpose(purpose))
            except (GatewayRefusedError, ProviderUnavailableError):
                continue
            available = True
            break
    degraded = AICircuit.objects.filter(
        closed_at__isnull=True,
        configuration__state="active",
        configuration__purpose__in=policy.purposes,
    ).exists()
    response = render(
        request,
        "ai/settings.html",
        {
            "form": form,
            "global_enabled": global_enabled,
            "organization_enabled": policy.enabled,
            "available": available,
            "degraded": degraded,
            "configured": current is not None,
        },
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response
