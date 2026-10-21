"""Longitudinal workspace: native forms and server-session patient selection."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import BOUND_ATTRIBUTE, bind_patient_context
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.services import ClinicalAccessDeniedError, decided
from apps.ehr.timeline import TimelineFilters
from apps.ehr.workspace_actions import action_result
from apps.ehr.workspace_presenter import TABS, empty_forms, workspace_context
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.tenancy.middleware import HANDLED_REJECTION_ATTR

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def continuation(clinic_id: UUID) -> str:
    """Patient selectors are deliberately absent from this continuation."""
    return reverse("ehr:patient-workspace", kwargs={"clinic_id": clinic_id})


def _filters(request: HttpRequest, clinic: Clinic) -> TimelineFilters:
    if clinic.timezone is None:
        raise ClinicalAccessDeniedError
    zone = ZoneInfo(clinic.timezone)
    start = request.POST.get("start", "") if request.method == "POST" else ""
    end = request.POST.get("end", "") if request.method == "POST" else ""
    episode = request.POST.get("episode_id", "") if request.method == "POST" else ""
    return TimelineFilters(
        kind=request.POST.get("kind", "") if request.method == "POST" else "",
        start=datetime.combine(date.fromisoformat(start), time.min, zone)
        if start
        else None,
        end=datetime.combine(
            date.fromisoformat(end) + timedelta(days=1),
            time.min,
            zone,
        )
        if end
        else None,
        episode_id=UUID(episode) if episode else None,
        offset=int(request.POST.get("offset", "0")),
    )


def _workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    require_current_actor_clinic_roles(
        clinic_id,
        (
            UserClinicRole.Role.PHYSICIAN,
            UserClinicRole.Role.NURSE,
            UserClinicRole.Role.ALLIED_PROFESSIONAL,
        ),
    )
    key = f"ehr.patient.{clinic_id}"
    selected = (
        request.POST.get("enrollment_id")
        if request.method == "POST"
        else request.session.get(key)
    )
    if not selected:
        if request.method == "POST":
            raise ClinicalAccessDeniedError
        return render(request, "ehr/patient_workspace.html", {"clinic_id": clinic_id})
    enrollment = UUID(str(selected))
    authority = partial(
        patient_scope, clinic_id=clinic_id, enrollment_id=enrollment, action="read"
    )
    scope = authority()
    tab = request.POST.get("tab", "overview")
    if tab not in {key for key, _label, _shortcut, available in TABS if available}:
        raise ClinicalAccessDeniedError
    forms = empty_forms()
    outcome = ""
    error = ""
    status = HTTPStatus.OK
    clinic = Clinic.objects.get(pk=clinic_id)
    try:
        filters = _filters(request, clinic)
    except (ValueError, ValidationError):
        filters = TimelineFilters()
        error = _("Choose a valid timeline period and type.")
        status = HTTPStatus.BAD_REQUEST
    with decided(authority):
        if request.method == "POST" and status == HTTPStatus.OK:
            outcome, error, status = action_result(
                scope=scope, data=request.POST, forms=forms
            )
        with transaction.atomic():
            context = workspace_context(
                scope=scope,
                tab=tab,
                filters=filters,
                forms=forms,
                trend_kind=request.POST.get("trend_kind", "temperature"),
            )
            context.update(error=error, outcome=outcome)
            context.update(
                filter_end=request.POST.get("end", ""),
                next_offset=filters.offset + filters.limit,
                previous_offset=max(0, filters.offset - filters.limit),
            )
            bind_patient_context(
                request,
                clinic_id=clinic_id,
                enrollment_id=enrollment,
                record_view=status == HTTPStatus.OK,
            )
            template = (
                "ehr/workspace/body.html"
                if request.headers.get("HX-Request") == "true"
                else "ehr/patient_workspace.html"
            )
            response = render(request, template, context, status=status)
            if status != HTTPStatus.OK:
                # Even a read-audit emitted while rendering a refused form rolls back.
                transaction.set_rollback(True)
                if hasattr(request, BOUND_ATTRIBUTE):
                    delattr(request, BOUND_ATTRIBUTE)
                setattr(response, HANDLED_REJECTION_ATTR, True)
    if status == HTTPStatus.OK:
        request.session[key] = str(enrollment)
    return response


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Fail identically for missing, other-patient and foreign clinical selectors."""
    try:
        return _workspace(request, clinic_id)
    except (ValueError, CurrentActorError, ClinicalAccessDeniedError):
        response = render(request, "403.html", status=403)
        if request.headers.get("HX-Request") == "true":
            response["HX-Retarget"] = "body"
            response["HX-Reselect"] = "body"
            response["HX-Reswap"] = "outerHTML"
        return response
