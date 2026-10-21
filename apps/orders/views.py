"""Keep result review, source intake and patient trends in explicit context."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.identity.otp import privileged_totp_required
from apps.intake.models import PatientClinicEnrollment
from apps.orders.commands import submit_command
from apps.orders.forms import OrderForm
from apps.orders.models import Order, ResultQuarantine
from apps.orders.results_presenter import prepare_reviews, prepare_uploads
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError
from apps.tenancy.middleware import HANDLED_REJECTION_ATTR

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest


def _patients(clinic_id: UUID) -> tuple[tuple[str, str], ...]:
    choices = []
    for enrollment in PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id
    ).select_related("patient")[:200]:
        try:
            require_permission(
                "order.place", clinic_id=clinic_id, patient_enrollment_id=enrollment.pk
            )
        except CurrentActorError:
            continue
        patient = enrollment.patient
        born = patient.birth_date
        label = born.strftime("%d/%m/%Y") if born else _("Date of birth unavailable")
        choices.append((str(enrollment.pk), f"{patient.full_name} - {label}"))
    return tuple(choices)


@privileged_totp_required(
    lambda clinic_id: reverse("orders:results", kwargs={"clinic_id": clinic_id})
)
@sensitive_post_parameters()
def results(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Keep selectors in POST bodies and recheck every displayed action."""
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
        clinic = Clinic.objects.get(pk=clinic_id)
        patients = _patients(clinic_id)
        outcome, error, status = "", "", HTTPStatus.OK
        if request.method not in {"GET", "POST"}:
            return HttpResponse(status=405)
        if request.method == "POST":
            try:
                with transaction.atomic():
                    outcome = submit_command(request, clinic_id, patients)
            except ValidationError as invalid:
                error, status = invalid.messages[0], HTTPStatus.BAD_REQUEST
            except ValueError:
                error, status = (
                    _("Review the result source and identity fields."),
                    HTTPStatus.BAD_REQUEST,
                )
            except OrdersConflictError:
                error, status = (
                    _(
                        "The source changed or requires review. "
                        "Refresh before continuing."
                    ),
                    HTTPStatus.CONFLICT,
                )
        orders = tuple(
            Order.objects.filter(clinic_id=clinic_id)
            .select_related("enrollment__patient")
            .order_by("-created_at")[:100]
        )
        invalid_upload = (
            request.method == "POST"
            and status == HTTPStatus.BAD_REQUEST
            and request.POST.get("action") == "upload"
        )
        result_forms, trend_groups = prepare_reviews(
            request, clinic_id, orders, invalid_upload=invalid_upload
        )
        try:
            require_permission("order.route", clinic_id=clinic_id)
            quarantine = tuple(
                ResultQuarantine.objects.filter(
                    clinic_id=clinic_id,
                    version__resultmatch__isnull=True,
                    version__resultrejection__isnull=True,
                ).select_related("version")
            )
        except CurrentActorError:
            quarantine = ()
        upload_forms = prepare_uploads(
            request, clinic_id, orders, invalid_upload=invalid_upload
        )
        order_form = OrderForm(
            patients,
            request.POST
            if request.method == "POST"
            and request.POST.get("action") == "place"
            and status == HTTPStatus.BAD_REQUEST
            else None,
        )
        if order_form.is_bound:
            order_form.is_valid()
        return render(
            request,
            "orders/results.html",
            {
                "clinic": clinic,
                "order_form": order_form,
                "patients_available": bool(patients),
                "uploads": upload_forms,
                "result_forms": result_forms,
                "trend_groups": trend_groups,
                "quarantines": quarantine,
                "outcome": outcome,
                "error": error,
                "render_key": uuid4().hex,
                "lab_enabled": settings.CLINIC_DATA_MODE == "synthetic"
                and settings.ORDERS_SYNTHETIC_LAB_ENABLED,
            },
            status=status,
        )
    except (CurrentActorError, OrdersAccessDeniedError):
        setattr(request, HANDLED_REJECTION_ATTR, True)
        return render(request, "403.html", status=403)
