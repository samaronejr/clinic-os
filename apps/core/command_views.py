"""Command palette and patient-context endpoints of the workspace shell.

Every endpoint takes record selectors and search text only in POST bodies;
paths carry nothing but the fixed route. ``command_page`` is the no-JavaScript
baseline of the palette; ``command_options`` renders the same results as
``<li role="option">`` rows for the combobox; ``command_run`` performs a
tokenized row (switching the patient in context); ``patient_close`` drops the
context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.core import command_tokens
from apps.core.command_search import (
    CommandQueryError,
    CommandResult,
    CommandSearch,
    search_commands,
)
from apps.core.navigation import granted_permissions
from apps.core.patient_context import (
    DEMOGRAPHICS_READ,
    PATIENT_BOUND_VIEWS,
    ContextSwitch,
    clear_patient_context,
    current_patient,
    discard_bound_work,
    set_patient_context,
    switch_consequences,
)
from apps.core.saved_views import destination_open, is_supported, spec_from_subject
from apps.core.workspace import WorkspaceClinic, current_clinic
from apps.identity.models import User
from apps.identity.otp import (
    is_confirmed_verified_user,
    is_privileged_user,
    privileged_totp_required,
)
from apps.identity.saved_views import SavedViewError, archive_saved_view, save_view
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase
    from django.http import HttpRequest, HttpResponseBase

PAGE_TEMPLATE: Final = "core/command.html"
OPTIONS_TEMPLATE: Final = "core/partials/command_options.html"
SWITCH_TEMPLATE: Final = "core/context_switch.html"
CONFLICT: Final = 409
FORBIDDEN: Final = 403


def command_continuation() -> str:
    """Resume a step-up challenge at the palette page (no search text kept)."""
    return reverse("workspace-command")


def _require_clinic(request: HttpRequest) -> WorkspaceClinic:
    clinic = current_clinic(request)
    if clinic is None:
        raise Http404
    return clinic


def tokenized(
    session: SessionBase, clinic_id: UUID, search: CommandSearch
) -> list[tuple[CommandResult, str]]:
    """Pair each row with the token that stands for its record selector."""
    rows: list[tuple[CommandResult, str]] = []
    for result in search.results:
        token = ""
        if result.subject is not None:
            token = command_tokens.issue(
                session, clinic_id=clinic_id, kind=result.kind, subject=result.subject
            )
        rows.append((result, token))
    return rows


def _grouped(rows: list[tuple[CommandResult, str]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for result, token in rows:
        groups.setdefault(result.group, []).append(
            {
                "value": token or result.action_url_name,
                "label": result.label,
                "meta": result.meta,
                "href": result.href or "",
                "kind": result.kind,
                "token": token,
            }
        )
    return [{"label": label, "items": items} for label, items in groups.items()]


def _search(request: HttpRequest, clinic: WorkspaceClinic) -> dict[str, object]:
    query = request.POST.get("q", "")
    context_path = request.POST.get("context", "")
    try:
        search = search_commands(
            clinic_id=clinic.id,
            timezone=clinic.timezone,
            roles=clinic.roles,
            query=query,
            context_path=context_path if context_path.startswith("/") else "",
        )
    except CommandQueryError:
        return {"query": "", "groups": [], "invalid": True, "patient_status": ""}
    return {
        "query": query,
        "groups": _grouped(tokenized(request.session, clinic.id, search)),
        "invalid": False,
        "patient_status": search.patient_status,
    }


@never_cache
@privileged_totp_required(command_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def command_page(request: HttpRequest) -> HttpResponseBase:
    """Serve the palette as a page: a native POST search form and its results."""
    clinic = _require_clinic(request)
    context: dict[str, object] = {"searched": request.method == "POST"}
    if request.method == "POST":
        context.update(_search(request, clinic))
    return render(request, PAGE_TEMPLATE, context)


@never_cache
@sensitive_post_parameters()
@require_POST
def command_options(request: HttpRequest) -> HttpResponseBase:
    """Render the combobox rows for one typed query (CSRF header required).

    A privileged actor without a confirmed second factor gets an empty 403
    instead of a redirect, so a sign-in page never lands inside the listbox.
    """
    user = request.user
    if not isinstance(user, User) or not user.is_authenticated:
        return HttpResponse(status=FORBIDDEN)
    if is_privileged_user(user) and not is_confirmed_verified_user(user):
        return HttpResponse(status=FORBIDDEN)
    clinic = _require_clinic(request)
    return render(request, OPTIONS_TEMPLATE, _search(request, clinic))


def _safe_next(request: HttpRequest) -> str:
    """Return the page to resume after a switch, never a patient-bound record."""
    candidate = request.POST.get("next", "")
    if not candidate.startswith("/") or not url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=False
    ):
        return reverse("workspace-home")
    try:
        match = resolve(candidate.split("?", 1)[0])
    except Resolver404:
        return reverse("workspace-home")
    if match.view_name in PATIENT_BOUND_VIEWS:
        return reverse("workspace-home")
    return candidate.split("?", 1)[0]


def _switch_patient(
    request: HttpRequest, clinic: WorkspaceClinic, token: str, enrollment_id: UUID
) -> HttpResponseBase:
    if DEMOGRAPHICS_READ not in granted_permissions(clinic.id) or not (
        PatientClinicEnrollment.objects.filter(
            pk=enrollment_id, clinic_id=clinic.id
        ).exists()
    ):
        raise Http404
    switch = ContextSwitch(
        clinic_id=clinic.id,
        from_enrollment_id=current_patient(request, clinic.id),
        to_enrollment_id=enrollment_id,
    )
    consequences = switch_consequences(switch)
    if consequences and request.POST.get("consequence") != "discard":
        return render(
            request,
            SWITCH_TEMPLATE,
            {
                "consequences": consequences,
                "token": token,
                "next": _safe_next(request),
            },
            status=CONFLICT,
        )
    if consequences:
        discard_bound_work(switch)
    set_patient_context(request, clinic_id=clinic.id, enrollment_id=enrollment_id)
    command_tokens.consume(request.session, token=token)
    return redirect(_safe_next(request))


@never_cache
@privileged_totp_required(command_continuation)
@sensitive_post_parameters()
@require_POST
def command_run(request: HttpRequest) -> HttpResponseBase:
    """Perform one tokenized palette row; every unusable token is the same 404."""
    clinic = _require_clinic(request)
    token = request.POST.get("token", "")
    found = command_tokens.peek(request.session, clinic_id=clinic.id, token=token)
    if found is None:
        raise Http404
    kind, subject = found
    if kind == "patient":
        try:
            enrollment_id = UUID(subject)
        except ValueError as error:
            raise Http404 from error
        return _switch_patient(request, clinic, token, enrollment_id)
    if kind in {"save_view", "archive_view"}:
        _change_saved_view(clinic, kind, subject)
        command_tokens.consume(request.session, token=token)
        return redirect(_safe_next(request))
    raise Http404


def _change_saved_view(clinic: WorkspaceClinic, kind: str, subject: str) -> None:
    """Save the page's view or archive one; any refusal is the same 404."""
    try:
        if kind == "archive_view":
            archive_saved_view(clinic_id=clinic.id, view_id=UUID(subject))
            return
        spec = spec_from_subject(subject)
        granted = granted_permissions(clinic.id)
        if (
            spec is None
            or not is_supported(spec)
            or not destination_open(spec, roles=clinic.roles, granted=granted)
        ):
            raise Http404
        save_view(clinic_id=clinic.id, destination=spec.destination, params=spec.params)
    except (ValueError, SavedViewError) as error:
        raise Http404 from error


@never_cache
@privileged_totp_required(command_continuation)
@require_POST
def patient_close(request: HttpRequest) -> HttpResponseBase:
    """Drop the clinic's patient context after the same switch guards."""
    clinic = _require_clinic(request)
    switch = ContextSwitch(
        clinic_id=clinic.id,
        from_enrollment_id=current_patient(request, clinic.id),
        to_enrollment_id=None,
    )
    consequences = switch_consequences(switch)
    if consequences and request.POST.get("consequence") != "discard":
        return render(
            request,
            SWITCH_TEMPLATE,
            {"consequences": consequences, "token": "", "next": _safe_next(request)},
            status=CONFLICT,
        )
    if consequences:
        discard_bound_work(switch)
    clear_patient_context(request, clinic_id=clinic.id)
    return redirect(_safe_next(request))
