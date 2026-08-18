"""Accessible POST-only patient search and registration screens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.http import Http404, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.intake.forms import (
    CONFLICTING_KEY_MESSAGE,
    INVALID_CREATE_MESSAGE,
    PatientCreateForm,
    PatientSearchForm,
)
from apps.intake.services import (
    PatientAccessDeniedError,
    PatientBirthDateError,
    PatientCreateInputError,
    PatientIdempotencyConflictError,
    PatientSearchInputError,
    create_patient,
    search_patients,
)

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from django.http import HttpRequest

    from apps.intake.services import PatientSearchPage

SEE_OTHER: Final = 303
NO_CONTENT: Final = 204
RESULTS_PARTIAL: Final = "intake/partials/patient_results.html"


def patient_list_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe search challenge at the blank clinic patient list."""
    return reverse("intake:patient-list", args=(clinic_id,))


def patient_create_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe registration challenge at the clinic create form."""
    return reverse("intake:patient-create", args=(clinic_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _search_context(
    clinic_id: UUID,
    form: PatientSearchForm,
    results: PatientSearchPage | None,
    birth_date: date | None,
) -> dict[str, object]:
    query = form.data.get("q", "") if results is not None else ""
    return {
        "birth_date_value": birth_date.isoformat() if birth_date else "",
        "clinic_id": clinic_id,
        "create_url": patient_create_continuation(clinic_id),
        "form": form,
        "list_url": patient_list_continuation(clinic_id),
        "next_page": (
            results.page + 1
            if results is not None and results.page < results.page_count
            else None
        ),
        "previous_page": (
            results.page - 1 if results is not None and results.page > 1 else None
        ),
        "query": query,
        "results": results,
    }


def _render_search(
    request: HttpRequest,
    context: dict[str, object],
) -> HttpResponseBase:
    template = RESULTS_PARTIAL if _is_htmx(request) else "intake/patient_list.html"
    return render(request, template, context)


@privileged_totp_required(patient_list_continuation)
@require_http_methods(["GET", "POST"])
def patient_list_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Render a blank search on GET and one deterministic page on POST."""
    if request.method != "POST":
        blank = PatientSearchForm()
        return render(
            request,
            "intake/patient_list.html",
            _search_context(clinic_id, blank, None, None),
        )
    form = PatientSearchForm(data=request.POST)
    results: PatientSearchPage | None = None
    birth_date: date | None = None
    if form.is_valid():
        birth_date = form.cleaned_data["birth_date"]
        try:
            results = search_patients(
                clinic_id=clinic_id,
                query=form.cleaned_data["q"],
                page=form.selected_page(),
                birth_date=birth_date,
            )
        except PatientAccessDeniedError as error:
            raise Http404 from error
        except PatientSearchInputError:
            form.add_error(None, INVALID_CREATE_MESSAGE)
    return _render_search(
        request,
        _search_context(clinic_id, form, results, birth_date),
    )


def _create_context(
    clinic_id: UUID,
    form: PatientCreateForm,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "clinic_id": clinic_id,
        "create_url": patient_create_continuation(clinic_id),
        "form": form,
        "idempotency_key": idempotency_key,
        "list_url": patient_list_continuation(clinic_id),
    }


def _created_response(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    target = patient_list_continuation(clinic_id)
    if _is_htmx(request):
        response: HttpResponseBase = HttpResponse(status=NO_CONTENT)
        response.headers["HX-Redirect"] = target
        return response
    return redirect(target, permanent=False)


@privileged_totp_required(patient_create_continuation)
@require_http_methods(["GET", "POST"])
def patient_create_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Issue one registration key on GET and register one patient on POST."""
    if request.method != "POST":
        blank = PatientCreateForm()
        return render(
            request,
            "intake/patient_create.html",
            _create_context(clinic_id, blank, str(uuid4())),
        )
    submitted_key = request.POST.get("idempotency_key", "")
    form = PatientCreateForm(data=request.POST)
    if form.is_valid():
        try:
            create_patient(
                clinic_id=clinic_id,
                full_name=form.cleaned_data["full_name"],
                birth_date=form.cleaned_data["birth_date"],
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        except PatientAccessDeniedError as error:
            raise Http404 from error
        except PatientIdempotencyConflictError:
            form.add_error(None, CONFLICTING_KEY_MESSAGE)
        except (PatientBirthDateError, PatientCreateInputError):
            form.add_error(None, INVALID_CREATE_MESSAGE)
        else:
            response = _created_response(request, clinic_id)
            if response.status_code != NO_CONTENT:
                response.status_code = SEE_OTHER
            return response
    return render(
        request,
        "intake/patient_create.html",
        _create_context(clinic_id, form, submitted_key),
    )
