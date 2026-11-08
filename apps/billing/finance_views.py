"""Native Finance workspace; persisted money state only, no PSP calls."""

from __future__ import annotations

from contextlib import nullcontext
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from django.contrib import messages
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.exports import export_journal
from apps.billing.finance_commands import execute_command
from apps.billing.finance_forms import ACTION_FIELDS, FinanceForm, JournalExportForm
from apps.billing.finance_queries import finance_context, finance_permissions
from apps.billing.fiscal_presentation import fiscal_summaries
from apps.billing.models import JournalBook
from apps.billing.money_types import JournalConflictError, JournalInputError
from apps.billing.security import OutgoingMoneyUnavailableError, finance_session
from apps.identity.otp import privileged_totp_required
from apps.identity.stepup import StepUpRequired

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase

STEP_UP_ACTIONS: Final = frozenset(
    {"approve_refund", "approve_payout", "refund_state", "payout_state", "reopen_cash"}
)
SUCCESS: Final = (
    "Financial command recorded. The displayed balances come from the journal."
)
REFUSED: Final = (
    "Financial action refused. Reload the current state before trying again."
)
STEP_UP: Final = "Verify your authenticator again before approving or reopening."
UNAVAILABLE: Final = "PSP and live-money actions remain blocked by external gates."


def _render(
    request: HttpRequest,
    clinic_id: UUID,
    *,
    error: str = "",
    status: int = HTTPStatus.OK,
    bound: FinanceForm | None = None,
) -> HttpResponse:
    context, choices = finance_context(clinic_id=clinic_id)
    context["fiscal_finance"], context["fiscal_rows"] = fiscal_summaries(
        clinic_id=clinic_id
    )
    permissions = finance_permissions(clinic_id)
    forms = []
    for action in ACTION_FIELDS:
        if not permissions[action] or (
            context["book"] is None and action != "create_book"
        ):
            continue
        if action == "create_book" and context["book"] is not None:
            continue
        form = (
            bound
            if bound is not None and bound.action == action
            else FinanceForm(action=action, choices=choices)
        )
        forms.append(form)
    context.update(
        forms=forms,
        error=error,
        export_form=JournalExportForm(),
        step_up_required=status == HTTPStatus.FORBIDDEN and error == _(STEP_UP),
    )
    template = (
        "billing/partials/finance_content.html"
        if request.headers.get("HX-Request") == "true"
        else "billing/finance.html"
    )
    return render(request, template, context, status=status)


def _command_scope(
    request: HttpRequest, clinic_id: UUID, action: str
) -> AbstractContextManager[None]:
    if action not in STEP_UP_ACTIONS:
        return nullcontext()
    key = request.session.session_key
    if key is None:
        raise StepUpRequired
    return finance_session(clinic_id=clinic_id, session_key=key)


def _apply_form(
    request: HttpRequest, clinic_id: UUID, form: FinanceForm
) -> tuple[str, HTTPStatus]:
    error, status = "", HTTPStatus.OK
    try:
        with _command_scope(request, clinic_id, form.action):
            execute_command(clinic_id=clinic_id, form=form)
    except StepUpRequired:
        error, status = _(STEP_UP), HTTPStatus.FORBIDDEN
    except BillingAccessDeniedError:
        error, status = _(REFUSED), HTTPStatus.FORBIDDEN
    except (JournalConflictError, JournalInputError, DatabaseError):
        error, status = _(REFUSED), HTTPStatus.CONFLICT
    except OutgoingMoneyUnavailableError:
        error, status = _(UNAVAILABLE), HTTPStatus.SERVICE_UNAVAILABLE
    return error, status


def _post(
    request: HttpRequest, clinic_id: UUID, permissions: Mapping[str, bool]
) -> HttpResponseBase:
    action = request.POST.get("action", "")
    if action not in ACTION_FIELDS or not permissions[action]:
        return _render(
            request, clinic_id, error=_(REFUSED), status=HTTPStatus.FORBIDDEN
        )
    _context, choices = finance_context(clinic_id=clinic_id)
    form = FinanceForm(request.POST, action=action, choices=choices)
    if not form.is_valid():
        return _render(
            request,
            clinic_id,
            error=_("Check the financial fields below."),
            status=HTTPStatus.BAD_REQUEST,
            bound=form,
        )
    error, status = _apply_form(request, clinic_id, form)
    if error:
        return _render(request, clinic_id, error=error, status=status, bound=form)
    messages.success(request, _(SUCCESS))
    if request.headers.get("HX-Request") == "true":
        return _render(request, clinic_id)
    return redirect("billing:finance", clinic_id=clinic_id)


def finance_url(clinic_id: UUID) -> str:
    """Resume an unsafe Finance action at the read-only workspace."""
    return reverse("billing:finance", kwargs={"clinic_id": clinic_id})


@never_cache
@privileged_totp_required(finance_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def finance_workspace(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Render a clinic workspace or apply one closed native command."""
    try:
        permissions = finance_permissions(clinic_id)
    except BillingAccessDeniedError:
        return render(request, "403.html", status=HTTPStatus.FORBIDDEN)
    if request.method == "POST":
        return _post(request, clinic_id, permissions)
    book = JournalBook.objects.filter(clinic_id=clinic_id).first()
    if book is not None:
        record_phase1_event(
            "billing.journal.viewed", clinic_id=clinic_id, affected_record_id=book.pk
        )
    return _render(request, clinic_id)


@never_cache
@privileged_totp_required(finance_url)
@sensitive_post_parameters()
@require_POST
def journal_export(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Download a journal projection by POST, never a cached identifier-bearing URL."""
    form = JournalExportForm(request.POST)
    if not form.is_valid():
        return HttpResponse(_("Check the export dates."), status=HTTPStatus.BAD_REQUEST)
    try:
        content = export_journal(
            clinic_id=clinic_id,
            start=form.cleaned_data["start"],
            end=form.cleaned_data["end"],
            output_format=form.cleaned_data["format"],
        )
    except BillingAccessDeniedError:
        return render(request, "403.html", status=HTTPStatus.FORBIDDEN)
    except JournalInputError:
        return HttpResponse(_("Check the export dates."), status=HTTPStatus.BAD_REQUEST)
    csv_format = form.cleaned_data["format"] == "csv"
    response = HttpResponse(
        content,
        content_type="text/csv; charset=utf-8" if csv_format else "application/xml",
    )
    extension = "csv" if csv_format else "xml"
    response["Content-Disposition"] = (
        f'attachment; filename="clinic-journal.{extension}"'
    )
    return response
