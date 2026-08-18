"""Accessible clinic availability screen with POST-only retirement safety."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.http import Http404, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.availability_presenter import (
    authorized_screen,
    manager_choices,
    presented_rows,
)
from apps.scheduling.forms import (
    CONFLICTING_KEY_MESSAGE,
    DEPENDENT_MESSAGE,
    INVALID_CREATE_MESSAGE,
    OVERLAP_MESSAGE,
    PRACTITIONER_MESSAGE,
    AvailabilityCreateForm,
)
from apps.scheduling.services import (
    AvailabilityAccessDeniedError,
    AvailabilityCreateInputError,
    AvailabilityHasAppointmentsError,
    AvailabilityIdempotencyConflictError,
    AvailabilityOverlapError,
    AvailabilityPractitionerError,
    create_availability,
    retire_availability,
    view_availability,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

    from apps.scheduling.availability_presenter import (
        AvailabilityRow,
        AvailabilityScreen,
    )

SEE_OTHER: Final = 303
NO_CONTENT: Final = 204
LIST_TEMPLATE: Final = "scheduling/availability_list.html"
BLOCKS_PARTIAL: Final = "scheduling/partials/availability_blocks.html"


def availability_list_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe create challenge at the clinic availability list."""
    return reverse("scheduling:availability-list", args=(clinic_id,))


def availability_retire_continuation(
    clinic_id: UUID,
    availability_id: UUID,  # noqa: ARG001 - the retired block never reaches a URL
) -> str:
    """Resume an unsafe retirement challenge at the clinic availability list."""
    return reverse("scheduling:availability-list", args=(clinic_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


@dataclass(frozen=True, slots=True)
class _ListedBlock:
    """Bind one presented block to its POST-only clinic retirement route."""

    retire_url: str
    row: AvailabilityRow


def _screen_context(
    clinic_id: UUID,
    screen: AvailabilityScreen,
    form: AvailabilityCreateForm | None,
    banner: str,
) -> dict[str, object]:
    items = view_availability(clinic_id=clinic_id)
    listed = [
        _ListedBlock(
            retire_url=reverse(
                "scheduling:availability-retire",
                args=(clinic_id, row.availability_id),
            ),
            row=row,
        )
        for row in presented_rows(clinic_id, items, screen)
    ]
    return {
        "banner": banner,
        "blocks": listed,
        "can_manage": screen.can_manage,
        "clinic_id": clinic_id,
        "form": form,
        "list_url": availability_list_continuation(clinic_id),
        "timezone_key": screen.timezone_key,
    }


def _render_screen(
    request: HttpRequest,
    context: dict[str, object],
    *,
    partial: bool,
) -> HttpResponseBase:
    template = BLOCKS_PARTIAL if partial and _is_htmx(request) else LIST_TEMPLATE
    return render(request, template, context)


def _completed_response(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    target = availability_list_continuation(clinic_id)
    if _is_htmx(request):
        response: HttpResponseBase = HttpResponse(status=NO_CONTENT)
        response.headers["HX-Redirect"] = target
        return response
    accepted = redirect(target, permanent=False)
    accepted.status_code = SEE_OTHER
    return accepted


def _created(form: AvailabilityCreateForm, clinic_id: UUID) -> bool:
    start_local, end_local = form.local_window()
    try:
        create_availability(
            clinic_id=clinic_id,
            practitioner_id=form.selected_practitioner(),
            start_local=start_local,
            end_local=end_local,
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except AvailabilityAccessDeniedError as error:
        raise Http404 from error
    except AvailabilityIdempotencyConflictError:
        form.add_error(None, CONFLICTING_KEY_MESSAGE)
    except AvailabilityOverlapError:
        form.add_error(None, OVERLAP_MESSAGE)
    except AvailabilityPractitionerError:
        form.add_error(None, PRACTITIONER_MESSAGE)
    except AvailabilityCreateInputError:
        form.add_error(None, INVALID_CREATE_MESSAGE)
    else:
        return True
    return False


@privileged_totp_required(availability_list_continuation)
@require_http_methods(["GET", "POST"])
def availability_list_view(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Render active clinic availability and accept one idempotent create."""
    try:
        screen = authorized_screen(clinic_id)
        if request.method != "POST":
            blank = (
                AvailabilityCreateForm(screen.choices, None)
                if screen.can_manage
                else None
            )
            if blank is not None:
                blank.initial["idempotency_key"] = str(uuid4())
            return _render_screen(
                request,
                _screen_context(clinic_id, screen, blank, ""),
                partial=False,
            )
        form = AvailabilityCreateForm(manager_choices(clinic_id), request.POST)
        if form.is_valid() and _created(form, clinic_id):
            return _completed_response(request, clinic_id)
        return _render_screen(
            request,
            _screen_context(clinic_id, screen, form, ""),
            partial=False,
        )
    except AvailabilityAccessDeniedError as error:
        raise Http404 from error


@privileged_totp_required(availability_retire_continuation)
@require_http_methods(["POST"])
def availability_retire_view(
    request: HttpRequest,
    clinic_id: UUID,
    availability_id: UUID,
) -> HttpResponseBase:
    """Retire one block whose stored clinic matches this authorized route."""
    try:
        retire_availability(clinic_id=clinic_id, availability_id=availability_id)
    except AvailabilityAccessDeniedError as error:
        raise Http404 from error
    except AvailabilityHasAppointmentsError:
        return _dependent_response(request, clinic_id)
    return _completed_response(request, clinic_id)


def _dependent_response(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    try:
        screen = authorized_screen(clinic_id)
        context = _screen_context(clinic_id, screen, None, DEPENDENT_MESSAGE)
    except AvailabilityAccessDeniedError as error:
        raise Http404 from error
    if screen.can_manage:
        context["form"] = AvailabilityCreateForm(screen.choices, None)
    return _render_screen(request, context, partial=True)
