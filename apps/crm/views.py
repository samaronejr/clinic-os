"""Native POST surfaces; public selectors never enter navigation URLs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.forms import PasswordInput
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.comms.inbox_services import InboxAccessDeniedError, InboxRefusedError
from apps.comms.models import TemplateVersion
from apps.crm.access import CrmAccessDeniedError, CrmRuleError, Reason, manager
from apps.crm.campaigns import (
    approve_campaign,
    campaign_audience,
    create_campaign,
    refresh_campaign,
    send_campaign,
)
from apps.crm.models import (
    Campaign,
    Lead,
    MarketingPreference,
    PublicBookingProfile,
    ReferralSource,
)
from apps.crm.patient import record_satisfaction, set_marketing_preference
from apps.crm.rules import MARKETING_BODIES, MAX_CODE
from apps.crm.services import (
    conversion_report,
    convert_lead,
    create_lead,
    create_source,
    publish_booking_profile,
    redact_expired_leads,
)
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError
from apps.intake.forms import PatientAccessForm
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    patient_session_context,
    redeem_invitation,
)
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.patient_booking import (
    book_patient_slot,
    patient_appointments,
    patient_services,
    patient_slots,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentPractitionerError,
    SlotConflict,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

PUBLIC_PROFILE_KEY = "crm_public_code"


@dataclass(frozen=True, slots=True)
class PublicBrand:
    """Only published clinic presentation; no patient or contact information."""

    clinic_id: UUID
    name: str
    description: str


def _brand(code: str) -> PublicBrand | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.crm_public_profile(%s)", [code])
        row = cursor.fetchone()
    return PublicBrand(*row) if row else None


@sensitive_post_parameters("code")
@require_http_methods(["GET", "POST"])
def public_access(request: HttpRequest) -> HttpResponse:
    """Redeem the existing invitation through a selector-free public URL."""
    code = request.session.get(PUBLIC_PROFILE_KEY, "")
    brand = _brand(code) if isinstance(code, str) and code else None
    if brand is None:
        return redirect("crm:public-booking")
    form = PatientAccessForm(data=request.POST if request.method == "POST" else None)
    form.fields["code"].widget = PasswordInput(attrs=form.fields["code"].widget.attrs)
    context: dict[str, object] = {"brand": brand, "access_form": form}
    if request.method == "POST" and form.is_valid():
        session_id = redeem_invitation(brand.clinic_id, form.selected_code())
        if session_id is not None:
            request.session.cycle_key()
            request.session[PATIENT_SESSION_KEY] = str(session_id)
            return redirect("crm:public-booking")
        context["error"] = gettext(
            "Access unavailable. Ask the clinic for a new invitation."
        )
    return render(request, "crm/public.html", context)


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def public_booking(request: HttpRequest) -> HttpResponse:
    """Show a real clinic brand and delegate every booking to self-booking."""
    if request.GET:
        return render(
            request,
            "crm/public.html",
            {"error": gettext("Use the form to select a clinic.")},
            status=400,
        )
    code = request.session.get(PUBLIC_PROFILE_KEY, "")
    code = code if isinstance(code, str) else ""
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if action == "clear":
        request.session.pop(PUBLIC_PROFILE_KEY, None)
        return redirect("crm:public-booking")
    if action == "select":
        code = request.POST.get("clinic_code", "")
        if len(code) > MAX_CODE or _brand(code) is None:
            return render(
                request,
                "crm/public.html",
                {
                    "error": gettext(
                        "Clinic unavailable. Check the code with the clinic."
                    )
                },
                status=404,
            )
        request.session[PUBLIC_PROFILE_KEY] = code
        return redirect("crm:public-booking")
    brand = _brand(code) if code else None
    context: dict[str, object] = {"brand": brand, "command_id": uuid4()}
    raw_session = request.session.get(PATIENT_SESSION_KEY)
    if brand is None or not isinstance(raw_session, str):
        if action:
            context["error"] = gettext("Verify your patient access before booking.")
        return render(
            request, "crm/public.html", context, status=403 if action else 200
        )
    return _public_patient_screen(request, raw_session, brand, context)


def _public_patient_screen(
    request: HttpRequest,
    raw_session: str,
    brand: PublicBrand,
    context: dict[str, object],
) -> HttpResponse:
    try:
        session_id = UUID(raw_session)
        with patient_session_context(session_id):
            _booking_fields(request, brand.clinic_id, context)
            return render(request, "crm/public.html", context)
    except (PatientAccessDeniedError, AppointmentAccessDeniedError):
        context["booking_ready"] = False
        context["error"] = gettext("Verify your patient access before booking.")
        return render(request, "crm/public.html", context, status=403)
    except (
        ValueError,
        AppointmentCreateInputError,
        AppointmentAvailabilityError,
        AppointmentPractitionerError,
        AppointmentIdempotencyConflictError,
        SlotConflict,
        SchedulingRuleError,
    ):
        context["error"] = gettext("The time is unavailable. Choose a new time.")
        return render(request, "crm/public.html", context, status=409)


def _booking_fields(
    request: HttpRequest, clinic_id: UUID, context: dict[str, object]
) -> None:
    scope = require_patient_booking_scope()
    if scope.clinic_id != clinic_id or set(request.POST) - {
        "csrfmiddlewaretoken",
        "action",
        "day",
        "service_type_id",
        "slot",
        "idempotency_key",
    }:
        raise AppointmentAccessDeniedError
    context["booking_ready"] = True
    context["services"] = patient_services()
    today = timezone.now().astimezone(ZoneInfo(scope.timezone)).date()
    day = date.fromisoformat(request.POST.get("day", today.isoformat()))
    context["day"] = day.isoformat()
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if action == "book":
        book_patient_slot(
            token=request.POST.get("slot", ""),
            idempotency_key=UUID(request.POST.get("idempotency_key", "")),
        )
        context["status"] = gettext("Booking recorded. Review it in your portal.")
    elif action not in {"", "slots"}:
        raise AppointmentCreateInputError
    raw_service = request.POST.get("service_type_id", "")
    service_id = UUID(raw_service) if raw_service else None
    context["slots"] = patient_slots(day, service_type_id=service_id)
    context["timezone"] = scope.timezone


@sensitive_post_parameters()
@privileged_totp_required(
    lambda clinic_id: reverse("crm:manager", kwargs={"clinic_id": clinic_id})
)
@require_http_methods(["GET", "POST"])
def manager_page(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Handle manager commands with every record selector in the POST body."""
    try:
        manager(clinic_id)
        return _manager_screen(request, clinic_id)
    except (CrmAccessDeniedError, InboxAccessDeniedError):
        return render(request, "403.html", status=403)


def _manager_screen(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    context: dict[str, object] = {"clinic": Clinic.objects.get(pk=clinic_id)}
    status = 200
    if request.method == "POST":
        try:
            with transaction.atomic():
                _manager_command(request, clinic_id, context)
            context["status"] = gettext("CRM action recorded.")
        except (
            ValueError,
            ValidationError,
            CrmRuleError,
            InboxRefusedError,
            IntegrityError,
        ):
            context["error"] = gettext(
                "Action refused. Check consent, neutral template and current revision."
            )
            status = 409
    redact_expired_leads(clinic_id=clinic_id)
    context["stats"] = conversion_report(clinic_id=clinic_id)
    context["leads"] = tuple(
        Lead.objects.filter(
            clinic_id=clinic_id, state="active", expires_at__gt=timezone.now()
        ).order_by("-created_at")[:200]
    )
    context["enrollments"] = tuple(
        PatientClinicEnrollment.objects.filter(clinic_id=clinic_id)
        .select_related("patient")
        .order_by("-created_at")[:200]
    )
    context["campaigns"] = tuple(
        Campaign.objects.filter(clinic_id=clinic_id).order_by("-created_at")[:50]
    )
    context["sources"] = tuple(ReferralSource.objects.filter(clinic_id=clinic_id))
    context["booking_profile"] = PublicBookingProfile.objects.filter(
        clinic_id=clinic_id
    ).first()
    context["templates"] = tuple(
        TemplateVersion.objects.filter(
            clinic_id=clinic_id,
            purpose="marketing",
            approval_status="approved",
            parameter_count=0,
            body__in=MARKETING_BODIES,
        )
    )
    return render(request, "crm/manager.html", context, status=status)


def _manager_command(
    request: HttpRequest, clinic_id: UUID, context: dict[str, object]
) -> None:
    action = request.POST.get("action", "")
    if action == "lead":
        raw_source = request.POST.get("source_id", "")
        create_lead(
            clinic_id=clinic_id,
            name=request.POST.get("name", ""),
            email=request.POST.get("email", ""),
            source_id=UUID(raw_source) if raw_source else None,
        )
    elif action == "source":
        create_source(
            clinic_id=clinic_id,
            code=request.POST.get("code", ""),
            name=request.POST.get("name", ""),
        )
    elif action == "profile":
        publish_booking_profile(
            clinic_id=clinic_id,
            code=request.POST.get("code", ""),
            description=request.POST.get("description", ""),
        )
    elif action == "convert":
        convert_lead(
            clinic_id=clinic_id,
            lead_id=UUID(request.POST.get("lead_id", "")),
            enrollment_id=UUID(request.POST.get("enrollment_id", "")),
        )
    elif action == "campaign":
        create_campaign(
            clinic_id=clinic_id,
            title=request.POST.get("title", ""),
            template_version_id=UUID(request.POST.get("template_version_id", "")),
        )
    else:
        campaign_id = UUID(request.POST.get("campaign_id", ""))
        if action == "preview":
            context["preview"] = campaign_audience(
                clinic_id=clinic_id, campaign_id=campaign_id
            )
        elif action == "refresh":
            refresh_campaign(clinic_id=clinic_id, campaign_id=campaign_id)
        elif action in {"approve", "send"}:
            command = approve_campaign if action == "approve" else send_campaign
            command(
                clinic_id=clinic_id,
                campaign_id=campaign_id,
                expected_revision=int(request.POST.get("expected_revision", "")),
            )
        else:
            raise CrmRuleError(Reason.INVALID_ACTION)


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def preferences(request: HttpRequest) -> HttpResponse:
    """Equal prominence opt-in and opt-out, neither a prerequisite for care."""
    context: dict[str, object] = {}
    status = 200
    try:
        if request.method == "POST":
            with transaction.atomic():
                _preference_command(request)
            context["status"] = gettext("Your marketing choice was recorded.")
        context["preferences"] = tuple(
            MarketingPreference.objects.order_by("channel", "-revision").distinct(
                "channel"
            )
        )
    except (CrmRuleError, DatabaseError):
        context["error"] = gettext(
            "Accept the current marketing text and verify the channel before opting in."
        )
        status = 409
    return render(request, "crm/preferences.html", context, status=status)


def _preference_command(request: HttpRequest) -> None:
    raw = request.POST.get("choice", "")
    if raw not in {"in", "out"}:
        raise CrmRuleError(Reason.INVALID_PREFERENCE)
    set_marketing_preference(
        channel=request.POST.get("channel", ""), opted_in=raw == "in"
    )


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def satisfaction(request: HttpRequest) -> HttpResponse:
    """Collect optional feedback without changing care or marketing consent."""
    context: dict[str, object] = {}
    status = 200
    try:
        context["timezone"] = require_patient_booking_scope().timezone
        if request.method == "POST":
            with transaction.atomic():
                record_satisfaction(
                    appointment_id=UUID(request.POST.get("appointment_id", "")),
                    score=int(request.POST.get("score", "")),
                )
            context["status"] = gettext(
                "Thank you. Your optional feedback was recorded."
            )
        context["appointments"] = tuple(
            a for a in patient_appointments() if a.status == "completed"
        )
    except (CrmRuleError, ValueError):
        context["error"] = gettext(
            "Choose a completed visit and a score from zero to ten."
        )
        status = 409
    except (PatientAccessDeniedError, AppointmentAccessDeniedError):
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=403
        )
    return render(request, "crm/satisfaction.html", context, status=status)
