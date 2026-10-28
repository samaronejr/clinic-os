"""POST-only encounter panel; no selection or refusal ever changes the session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
)
from apps.ai.decision_forms import DecisionSupportForm
from apps.ai.decision_support import (
    DecisionPanel,
    display_decision_support,
    open_decision_support,
    record_decision,
    request_decision_support,
)
from apps.core.workspace import finish_clinic_selection
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse


def _continuation(clinic_id: UUID) -> str:
    return reverse("ehr:encounter", args=(clinic_id,))


@never_cache
@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_POST
def decision_support(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Keep missing, foreign and unauthorized encounter refusals indistinguishable."""
    form = DecisionSupportForm(request.POST)
    if (
        set(request.POST) - {*form.fields, "csrfmiddlewaretoken"}
        or request.GET
        or request.FILES
        or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        or not form.is_valid()
    ):
        return render(request, "400.html", status=400)
    if not request.session.session_key:
        return render(request, "403.html", status=403)
    data = form.cleaned_data
    binding = salted_hmac(
        "ai.decision-session.v1", request.session.session_key, algorithm="sha256"
    ).hexdigest()
    try:
        if data["command"] == "open":
            panel = open_decision_support(
                clinic_id=clinic_id, encounter_id=data["encounter_id"]
            )
        elif data["command"] == "generate":
            panel = request_decision_support(
                clinic_id=clinic_id,
                encounter_id=data["encounter_id"],
                session_binding=binding,
                opt_in=data["opt_in"],
            )
        elif data["command"] == "display":
            panel = display_decision_support(
                clinic_id=clinic_id,
                encounter_id=data["encounter_id"],
                session_binding=binding,
                invocation_id=data["invocation_id"],
            )
        else:
            try:
                panel = record_decision(
                    clinic_id=clinic_id,
                    encounter_id=data["encounter_id"],
                    session_binding=binding,
                    invocation_id=data["invocation_id"],
                    decision=data["command"],
                )
            except GatewayRefusedError:
                panel = DecisionPanel()
    except (CurrentActorError, ClinicalAccessDeniedError, GatewayAccessDeniedError):
        return render(request, "403.html", status=403)
    except GatewayInputError:
        return render(request, "400.html", status=400)
    response = render(
        request,
        "ai/decision_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "ai/decision.html",
        {
            "clinic_id": clinic_id,
            "encounter_id": data["encounter_id"],
            "cds": panel,
            "clinic_timezone": Clinic.objects.get(pk=clinic_id).timezone,
        },
    )
    finish_clinic_selection(request, accepted=False)
    return response
