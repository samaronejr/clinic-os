"""Multi-resource agenda board routes and its move command (HTMX over forms).

The board routes carry only the clinic, the civil view and the civil date. The
move POST carries the appointment, the revision the board rendered and the new
clinic-local start in its body. Unknown, foreign and unauthorized
appointments share one 404 and write nothing; a move that loses a race or
meets a taken time answers 409 with the current board, never a silent write.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.contrib import messages
from django.http import Http404, HttpResponseBase, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.agenda_board import BOARD_VIEWS
from apps.scheduling.agenda_board_presenter import board_url
from apps.scheduling.agenda_board_views import apply_board_preferences, render_board
from apps.scheduling.agenda_grid import authorize_grid_move
from apps.scheduling.appointment_forms import (
    BOOKING_PRACTITIONER_MESSAGE,
    INVALID_RESCHEDULE_MESSAGE,
    SLOT_MESSAGE,
    TERMINAL_MESSAGE,
    WINDOW_MESSAGE,
)
from apps.scheduling.appointment_lifecycle import AppointmentLifecycleError
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.models import Appointment
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    AvailabilityAccessDeniedError,
    SlotConflict,
    move_appointment,
    view_appointment_for_transition,
)

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.utils.functional import Promise

CONFLICT_STATUS: Final = 409
SEE_OTHER: Final = 303
MOVED_TAG: Final = "scheduling.appointment.moved"
FIELDS: Final = frozenset(
    {
        "csrfmiddlewaretoken",
        "appointment_id",
        "expected_revision",
        "day",
        "start",
        "duration",
        "select",
        "view",
    }
)
CLOCK: Final = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
MAX_DURATION: Final = 720
MOVED_MESSAGE: Final = _("Appointment moved to %(start)s.")
REVISION_MESSAGE: Final = _(
    "Someone else changed this appointment first. The grid shows their "
    "change; yours was not saved."
)


def grid_continuation(clinic_id: UUID, day: str, view: str = "day") -> str:
    """Resume a board challenge at the same civil view and clinic-local day."""
    selected = view if view in BOARD_VIEWS else "day"
    try:
        return board_url(clinic_id, selected, date.fromisoformat(day))
    except ValueError:
        return reverse("scheduling:agenda", args=(clinic_id,))


def move_continuation(clinic_id: UUID) -> str:
    """Resume a move challenge at the clinic agenda, never replaying the POST."""
    return reverse("scheduling:agenda", args=(clinic_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


@privileged_totp_required(grid_continuation)
@require_http_methods(["GET", "POST"])
def agenda_grid_view(
    request: HttpRequest, clinic_id: UUID, day: str, view: str = "day"
) -> HttpResponseBase:
    """Render one authorized board view; a POST changes filters or saved views."""
    if request.method == "POST":
        return apply_board_preferences(request, clinic_id, view, day)
    return render_board(request, clinic_id, view, day)


def _inputs(
    request: HttpRequest,
) -> tuple[UUID, int, str, str, AppointmentLocalRange]:
    if set(request.POST) - FIELDS:
        raise Http404
    start = request.POST.get("start", "")
    day = request.POST.get("day", "")
    view = request.POST.get("view", "day")
    try:
        appointment_id = UUID(request.POST.get("appointment_id", ""))
        revision = int(request.POST.get("expected_revision", ""))
        duration = int(request.POST.get("duration", ""))
        begin = datetime.fromisoformat(f"{day}T{start}")
    except ValueError as error:
        raise Http404 from error
    if (
        CLOCK.fullmatch(start) is None
        or begin.date().isoformat() != day
        or not 1 <= duration <= MAX_DURATION
        or view not in BOARD_VIEWS
    ):
        raise Http404
    end = begin + timedelta(minutes=duration)
    local_range = AppointmentLocalRange(
        begin.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")
    )
    return appointment_id, revision, day, view, local_range


REFUSED: Final = (
    AppointmentLifecycleError,
    AppointmentTerminalError,
    SlotConflict,
    AppointmentAvailabilityError,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
)
REFUSALS: Final[dict[type[Exception], Promise]] = {
    AppointmentLifecycleError: REVISION_MESSAGE,
    AppointmentTerminalError: TERMINAL_MESSAGE,
    SlotConflict: SLOT_MESSAGE,
    AppointmentAvailabilityError: WINDOW_MESSAGE,
    AppointmentPractitionerError: BOOKING_PRACTITIONER_MESSAGE,
    AppointmentRescheduleInputError: INVALID_RESCHEDULE_MESSAGE,
}


@privileged_totp_required(move_continuation)
@require_http_methods(["POST"])
def agenda_grid_move_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Move one appointment only from the revision the grid rendered.

    The grid's own scope is authorized before any write: a refusal (unknown or
    foreign clinic, an actor outside the agenda, an unknown appointment)
    is the unknown-clinic 404 and changes nothing.
    """
    appointment_id, revision, day, view, local_range = _inputs(request)
    try:
        authorize_grid_move(clinic_id)
        if "select" in request.POST:
            # Check the explicit clinic before the audited transition read.
            # Unknown and other-clinic selectors must both write nothing.
            if not Appointment.objects.filter(
                pk=appointment_id,
                clinic_id=clinic_id,
                status=Appointment.Status.SCHEDULED,
            ).exists():
                raise Http404
            current = view_appointment_for_transition(appointment_id=appointment_id)
            return render(
                request,
                "scheduling/agenda_move.html",
                {
                    "appointment": current,
                    "appointment_id": appointment_id,
                    "expected_revision": revision,
                    "day": day,
                    "start": local_range.start_local[11:16],
                    "duration": request.POST["duration"],
                    "view": view,
                    "move_url": reverse(
                        "scheduling:agenda-grid-move", args=(clinic_id,)
                    ),
                    "action_url": reverse(
                        "scheduling:agenda-grid-action", args=(clinic_id,)
                    ),
                    "grid_url": board_url(clinic_id, view, date.fromisoformat(day)),
                },
            )
        move_appointment(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            expected_revision=revision,
            local_range=local_range,
        )
    except (AppointmentAccessDeniedError, AvailabilityAccessDeniedError) as error:
        raise Http404 from error
    except SchedulingRuleError as error:
        refusal = str(error.message)
    except REFUSED as error:
        refusal = next(
            str(text) for kind, text in REFUSALS.items() if isinstance(error, kind)
        )
    else:
        moved = str(MOVED_MESSAGE % {"start": local_range.start_local[11:16]})
        if _is_htmx(request):
            return render_board(
                request, clinic_id, view, day, notice=(moved, "success")
            )
        messages.success(request, moved, extra_tags=MOVED_TAG)
        accepted = HttpResponseRedirect(
            board_url(clinic_id, view, date.fromisoformat(day))
        )
        accepted.status_code = SEE_OTHER
        return accepted
    return render_board(
        request,
        clinic_id,
        view,
        day,
        notice=(refusal, "warning"),
        status=CONFLICT_STATUS,
    )
