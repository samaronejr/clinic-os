"""Keep emergency access and patient transparency free of URL selectors."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import connection
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.identity.account_views import request_session_key
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.enterprise_access import (
    EmergencyScope,
    grant_break_glass,
    read_break_glass,
)
from apps.identity.enterprise_authority import GovernanceDeniedError, factor_scope
from apps.identity.enterprise_forms import EmergencyAccessForm, GrantCommandForm
from apps.identity.models import BreakGlassGrant, Clinic
from apps.identity.otp import auth_response, privileged_totp_required

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase

MAX_ACCESS_LOG_PAGE_DIGITS = 5


@require_http_methods(["GET"])
@privileged_totp_required(
    lambda clinic_id: reverse("identity:emergency-reviews", args=(clinic_id,))
)
def emergency_reviews(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Let a unit manager inspect a reason without granting organization powers."""
    with (
        factor_scope(session_key=request_session_key(request)),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.enterprise_manager(%s)", [clinic_id])
        if cursor.fetchone() != (True,):
            raise GovernanceDeniedError
        clinic = Clinic.objects.filter(pk=clinic_id).first()
        if clinic is None:
            raise GovernanceDeniedError
        return auth_response(
            render(
                request,
                "identity/emergency_reviews.html",
                {
                    "clinic": clinic,
                    "clinic_id": clinic_id,
                    "review_now": timezone.now(),
                    "emergencies": tuple(
                        BreakGlassGrant.objects.filter(clinic_id=clinic_id).order_by(
                            "-created_at"
                        )[:100]
                    ),
                },
            )
        )


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
@privileged_totp_required(
    lambda clinic_id: reverse("identity:emergency", args=(clinic_id,))
)
def emergency_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Request a reason-bound grant without adding clinical write authority."""
    try:
        require_permission("break_glass.request", clinic_id=clinic_id)
    except CurrentActorError:
        require_permission("break_glass.request_scoped", clinic_id=clinic_id)
    form = EmergencyAccessForm(request.POST if request.method == "POST" else None)
    granted = False
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        try:
            grant_break_glass(
                clinic_id=clinic_id,
                scope=EmergencyScope(values["enrollment_id"], values["encounter_id"]),
                reason=values["reason"],
                expires_at=timezone.now() + timedelta(minutes=values["minutes"]),
                session_key=request_session_key(request),
            )
        except GovernanceDeniedError:
            form.add_error(
                None,
                _(
                    "Access refused. Check the scope and confirm "
                    "your authenticator again."
                ),
            )
        else:
            granted = True
    return auth_response(
        render(
            request,
            "identity/emergency.html",
            {
                "clinic_id": clinic_id,
                "form": form,
                "granted": granted,
                "grants": tuple(
                    BreakGlassGrant.objects.filter(clinic_id=clinic_id).order_by(
                        "-created_at"
                    )[:100]
                ),
            },
            status=400 if form.errors else 200,
        )
    )


@require_POST
@privileged_totp_required(
    lambda clinic_id: reverse("identity:emergency", args=(clinic_id,))
)
def emergency_read(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Render only the specific live grant's finalized ordinary notes/observations."""
    form = GrantCommandForm(request.POST)
    if not form.is_valid():
        return auth_response(render(request, "400.html", status=400))
    snapshot = read_break_glass(
        clinic_id=clinic_id, grant_id=form.cleaned_data["grant_id"]
    )
    return auth_response(
        render(
            request,
            "identity/emergency_read.html",
            {"snapshot": snapshot, "clinic_id": clinic_id},
        )
    )


@require_http_methods(["GET", "POST"])
def patient_access_log(request: HttpRequest) -> HttpResponseBase:
    """Resolve the stored patient scope without admitting delegates."""
    page_text = request.POST.get("page", "0") if request.method == "POST" else "0"
    if (
        not page_text.isascii()
        or not page_text.isdigit()
        or len(page_text) > MAX_ACCESS_LOG_PAGE_DIGITS
    ):
        return auth_response(render(request, "400.html", status=400))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_id FROM "
            "clinic_app.current_patient_authority('enrollment_view')"
            " WHERE delegate_grant_id IS NULL"
        )
        scope = cursor.fetchone()
        if scope is None:
            return auth_response(render(request, "403.html", status=403))
        page = int(page_text)
        cursor.execute(
            "SELECT * FROM clinic_app.enterprise_patient_access_log(%s,%s)",
            [scope[0], page],
        )
        entries = [
            {
                "occurred_at": row[1],
                "clinician": row[2],
                "expires_at": row[3],
                "reviewed": row[4],
                "timezone": row[5],
            }
            for row in cursor.fetchall()
        ]
    return auth_response(
        render(
            request,
            "identity/patient_access_log.html",
            {
                "entries": entries,
                "previous_page": max(0, page - 1),
                "next_page": page + 1,
            },
        )
    )
