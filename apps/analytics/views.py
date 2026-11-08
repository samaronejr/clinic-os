"""One native Reports workspace with an optional HTMX rendering enhancement."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from django.db import transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ai.contracts import GatewayAccessDeniedError, GatewayInputError
from apps.analytics.analyst import (
    Proposal,
    display_proposal,
    request_analyst,
    review_proposal,
)
from apps.analytics.catalog import METRICS
from apps.analytics.contracts import (
    AnalyticsAccessDeniedError,
    AnalyticsConflictError,
    AnalyticsInputError,
    Query,
    Result,
)
from apps.analytics.forms import ReferenceForm, ReportForm
from apps.analytics.models import SavedReport
from apps.analytics.services import (
    authorize,
    definitions,
    drill_down,
    load_report,
    query_metric,
    refresh_metric,
    save_report,
)
from apps.core.workspace import DENIED_ATTRIBUTE
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse


def reports_url(clinic_id: UUID) -> str:
    """Unsafe requests resume at the read-only Reports workspace."""
    return reverse("analytics:reports", kwargs={"clinic_id": clinic_id})


def _reference_command(
    request: HttpRequest, clinic_id: UUID, action: str
) -> dict[str, object]:
    reference = ReferenceForm(request.POST)
    if not reference.is_valid():
        raise AnalyticsInputError
    identifier = reference.cleaned_data["reference"]
    if action == "load":
        query = load_report(clinic_id=clinic_id, report_id=identifier)
        return {
            "query": query,
            "result": query_metric(clinic_id=clinic_id, query=query),
        }
    if action == "proposal":
        return {
            "proposal": display_proposal(clinic_id=clinic_id, invocation_id=identifier)
        }
    result = review_proposal(
        clinic_id=clinic_id,
        invocation_id=identifier,
        decision=action,
        query_digest=reference.cleaned_data["query_digest"],
        authorization_version=reference.cleaned_data["authorization_version"],
    )
    return {"result": result, "notice": _("Human decision recorded.")}


def _command(
    request: HttpRequest, clinic_id: UUID, form: ReportForm
) -> dict[str, object]:
    action = request.POST.get("action", "")
    if action in {"proposal", "accepted", "rejected", "load"}:
        return _reference_command(request, clinic_id, action)
    if not form.is_valid():
        return {
            "error": _("Check the report fields."),
            "status": HTTPStatus.BAD_REQUEST,
        }
    query = form.query
    if action == "ask":
        return {
            "proposal": request_analyst(
                clinic_id=clinic_id, question=form.cleaned_data["question"], query=query
            )
        }
    if action == "refresh":
        return {
            "result": refresh_metric(
                clinic_id=clinic_id, query=query, command_id=uuid4()
            )
        }
    if action == "save":
        save_report(clinic_id=clinic_id, query=query)
        return {
            "result": query_metric(clinic_id=clinic_id, query=query),
            "notice": _("Report saved."),
        }
    if action in {"cached", "drill"}:
        context: dict[str, object] = {
            "result": query_metric(clinic_id=clinic_id, query=query)
        }
        if action == "drill":
            context["drill"] = drill_down(clinic_id=clinic_id, query=query)
        return context
    raise AnalyticsInputError


def _render(
    request: HttpRequest,
    clinic_id: UUID,
    form: ReportForm,
    context: dict[str, object],
) -> HttpResponse:
    actor, _version = authorize(clinic_id=clinic_id)
    clinic = Clinic.objects.get(pk=clinic_id)
    result = context.get("result")
    proposal = context.get("proposal")
    query = (
        result.query
        if isinstance(result, Result)
        else (
            proposal.query
            if isinstance(proposal, Proposal) and proposal.query
            else context.get("query", getattr(form, "query", None))
        )
    )
    selected = query.metric_id if isinstance(query, Query) else "no_show"
    if isinstance(query, Query) and not form.is_bound:
        form.select_query(query)
    context.update(
        clinic_id=clinic_id,
        clinic_timezone=str(clinic.timezone),
        form=form,
        definitions=definitions(clinic_id=clinic_id),
        selected=METRICS[selected],
        saved=[
            {
                "pk": report.pk,
                "label": METRICS[report.definition.metric_id].label,
                "created_at": report.created_at,
            }
            for report in SavedReport.objects.filter(clinic_id=clinic_id, user_id=actor)
            .select_related("definition")
            .order_by("-created_at")[:20]
        ],
    )
    if isinstance(proposal, Proposal) and proposal.query is not None:
        weekdays = (
            _("Monday"),
            _("Tuesday"),
            _("Wednesday"),
            _("Thursday"),
            _("Friday"),
            _("Saturday"),
            _("Sunday"),
        )
        context.update(
            proposal_grouping=_("Day") if proposal.query.dimensions else _("Total"),
            proposal_days=", ".join(
                weekdays[index] for index in proposal.query.weekdays
            )
            or _("Every day"),
        )
    template = (
        "analytics/partials/reports.html"
        if request.headers.get("HX-Request") == "true"
        else "analytics/reports.html"
    )
    return render(
        request,
        template,
        context,
        status=cast("int", context.get("status", HTTPStatus.OK)),
    )


@never_cache
@privileged_totp_required(reports_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def reports(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Roll back commands if either their own or their response authority fails."""
    try:
        with transaction.atomic():
            authorize(clinic_id=clinic_id)
            clinic = Clinic.objects.get(pk=clinic_id)
            reference_action = request.POST.get("action") in {
                "proposal",
                "accepted",
                "rejected",
                "load",
            }
            form = ReportForm(
                request.POST
                if request.method == "POST" and not reference_action
                else None,
                clinic_timezone=str(clinic.timezone),
            )
            context = (
                _command(request, clinic_id, form) if request.method == "POST" else {}
            )
            return _render(request, clinic_id, form, context)
    except (AnalyticsInputError, AnalyticsConflictError, GatewayInputError):
        context = {
            "error": _("The report request was refused. Reload Reports and try again."),
            "status": HTTPStatus.CONFLICT,
        }
        return _render(
            request,
            clinic_id,
            ReportForm(clinic_timezone=str(clinic.timezone)),
            context,
        )
    except (AnalyticsAccessDeniedError, GatewayAccessDeniedError):
        setattr(request, DENIED_ATTRIBUTE, True)
        template = (
            "analytics/partials/denied.html"
            if request.headers.get("HX-Request") == "true"
            else "403.html"
        )
        return render(request, template, status=HTTPStatus.FORBIDDEN)
