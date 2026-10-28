"""A single POST-only chart drawer on Today and the clinical workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.ai.brief import display_brief, request_brief
from apps.ai.brief_forms import ChartQuestionForm
from apps.ai.contracts import GatewayAccessDeniedError, GatewayInputError
from apps.core.patient_context import bind_patient_context
from apps.ehr.chart_sources import chart_enrollment
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.identity.otp import privileged_totp_required
from apps.workflows.today import view_today

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse


def _continuation(clinic_id: UUID) -> str:
    return reverse("ehr:patient-workspace", args=(clinic_id,))


@never_cache
@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_POST
def chart_assistant(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Unknown and unauthorized records return the same content-free refusal."""
    form = ChartQuestionForm(request.POST)
    if (
        set(request.POST) - {*form.fields, "csrfmiddlewaretoken"}
        or request.FILES
        or request.GET
        or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        or not form.is_valid()
    ):
        return render(request, "400.html", status=400)
    data = form.cleaned_data
    try:
        enrolled = chart_enrollment(
            clinic_id=clinic_id,
            enrollment_id=data["enrollment_id"],
            encounter_id=data["encounter_id"],
        )
        today = view_today(clinic_id=clinic_id) if data["surface"] == "today" else None
        if today is not None and (
            today.next_patient is None or today.next_patient.enrollment_id != enrolled
        ):
            return render(request, "403.html", status=403)
        if not request.session.session_key:
            return render(request, "403.html", status=403)
        binding = salted_hmac(
            "ai.chart-session.v1", request.session.session_key, algorithm="sha256"
        ).hexdigest()
        if data["command"] == "display":
            brief = display_brief(
                clinic_id=clinic_id,
                enrollment_id=enrolled,
                session_binding=binding,
                invocation_id=data["invocation_id"],
            )
        else:
            brief = request_brief(
                clinic_id=clinic_id,
                enrollment_id=enrolled,
                session_binding=binding,
                question=data["question"] if data["command"] == "ask" else "",
            )
        context: dict[str, object] = {
            "clinic_id": clinic_id,
            "chart_brief": brief,
            "chart_enrollment_id": enrolled,
            "chart_surface": data["surface"],
        }
        bind_patient_context(
            request, clinic_id=clinic_id, enrollment_id=enrolled, record_view=False
        )
        if request.headers.get("HX-Request") == "true":
            template = "ai/brief_fragment.html"
        elif data["surface"] == "today":
            context.update(
                screen=today,
                inbox_url=reverse("workflows:inbox", args=(clinic_id,)),
                action_url=reverse("workflows:today", args=(clinic_id,)),
                realtime_enabled=settings.REALTIME_ENABLED,
                realtime_polling=settings.REALTIME_POLLING_FALLBACK,
            )
            template = "workflows/today.html"
        else:
            template = "ai/brief.html"
        return render(request, template, context)
    except (CurrentActorError, ClinicalAccessDeniedError, GatewayAccessDeniedError):
        return render(request, "403.html", status=403)
    except GatewayInputError:
        return render(request, "400.html", status=400)
