"""Multi-resource day grid and its move command (HTMX over native forms).

The grid route carries only the clinic and the civil date. The move POST
carries the appointment, the revision the grid rendered and the new
clinic-local start in its body. Unknown, foreign and unauthorized
appointments share one 404 and write nothing; a move that loses a race or
meets a taken time answers 409 with the current grid, never a silent write.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseBase, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.agenda_grid import (
    SLOT_MINUTES,
    DayGrid,
    GridAppointment,
    GridColumn,
    authorize_grid_move,
    view_day_grid,
)
from apps.scheduling.agenda_presenter import clinic_local_today
from apps.scheduling.appointment_forms import (
    BOOKING_PRACTITIONER_MESSAGE,
    INVALID_RESCHEDULE_MESSAGE,
    SLOT_MESSAGE,
    TERMINAL_MESSAGE,
    WINDOW_MESSAGE,
)
from apps.scheduling.appointment_lifecycle import AppointmentLifecycleError
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import (
    AgendaInputError,
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    AvailabilityAccessDeniedError,
    SlotConflict,
    move_appointment,
)

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.utils.functional import Promise

GRID_TEMPLATE: Final = "scheduling/agenda_grid.html"
GRID_PARTIAL: Final = "scheduling/partials/agenda_grid.html"
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
    }
)
CLOCK: Final = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
MAX_DURATION: Final = 720
MOVED_MESSAGE: Final = _("Appointment moved to %(start)s.")
REVISION_MESSAGE: Final = _(
    "Someone else changed this appointment first. The grid shows their "
    "change; yours was not saved."
)


def grid_continuation(clinic_id: UUID, day: str) -> str:
    """Resume a grid challenge at the same clinic-local day."""
    return _grid_url(clinic_id, day)


def move_continuation(clinic_id: UUID) -> str:
    """Resume a move challenge at the clinic agenda, never replaying the POST."""
    return reverse("scheduling:agenda", args=(clinic_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _grid_url(clinic_id: UUID, day: str) -> str:
    return reverse("scheduling:agenda-grid", args=(clinic_id, day))


def _neighbours(clinic_id: UUID, grid: DayGrid) -> dict[str, str]:
    return {
        "previous_url": _grid_url(
            clinic_id, (grid.date - timedelta(days=1)).isoformat()
        ),
        "next_url": _grid_url(clinic_id, (grid.date + timedelta(days=1)).isoformat()),
        "today_url": _grid_url(clinic_id, clinic_local_today(grid.timezone_key)),
        "list_url": reverse(
            "scheduling:agenda-at", args=(clinic_id, "day", grid.date.isoformat(), 1)
        ),
    }


def _minute(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _in_column(item: GridAppointment, column: GridColumn) -> bool:
    if column.kind == "practitioner":
        return item.practitioner_id == column.column_id
    return column.column_id in item.room_ids


def _cell(grid: DayGrid, column: GridColumn, slot: int) -> dict[str, object]:
    starting: list[GridAppointment] = []
    covering = False
    for item in grid.appointments:
        if not _in_column(item, column):
            continue
        start = _minute(item.start_time)
        end = _minute(item.end_time)
        if slot <= start < slot + SLOT_MINUTES:
            starting.append(item)
        elif start < slot < end:
            covering = True
    kind = "booked" if starting else "continues" if covering else "free"
    return {"kind": kind, "column": column, "appointments": starting}


def grid_context(clinic_id: UUID, grid: DayGrid) -> dict[str, object]:
    """Lay every appointment into the (slot x column) cells the template walks."""
    names = {column.column_id: column.label for column in grid.columns}
    rows = [
        {
            "time": time,
            "cells": [_cell(grid, column, _minute(time)) for column in grid.columns],
        }
        for time in grid.slots
    ]
    return {
        "clinic_id": clinic_id,
        "grid": grid,
        "rows": rows,
        "names": names,
        "practitioner_count": sum(
            1 for column in grid.columns if column.kind == "practitioner"
        ),
    }


def _render(
    request: HttpRequest,
    clinic_id: UUID,
    day: str,
    *,
    notice: tuple[str, str] = ("", ""),
    status: int = 200,
) -> HttpResponseBase:
    try:
        grid = view_day_grid(clinic_id=clinic_id, date=day)
    except (AvailabilityAccessDeniedError, AgendaInputError) as error:
        raise Http404 from error
    context = {
        **grid_context(clinic_id, grid),
        **_neighbours(clinic_id, grid),
        "grid_url": _grid_url(clinic_id, day),
        "realtime_enabled": settings.REALTIME_ENABLED,
        "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        "move_url": reverse("scheduling:agenda-grid-move", args=(clinic_id,)),
        "notice": notice[0],
        "notice_tone": notice[1],
    }
    template = GRID_PARTIAL if _is_htmx(request) else GRID_TEMPLATE
    return render(request, template, context, status=status)


@privileged_totp_required(grid_continuation)
@require_http_methods(["GET"])
def agenda_grid_view(
    request: HttpRequest, clinic_id: UUID, day: str
) -> HttpResponseBase:
    """Render one authorized clinic-local day by practitioner and room."""
    return _render(request, clinic_id, day)


def _inputs(request: HttpRequest) -> tuple[UUID, int, str, AppointmentLocalRange]:
    if set(request.POST) - FIELDS:
        raise Http404
    start = request.POST.get("start", "")
    day = request.POST.get("day", "")
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
    ):
        raise Http404
    end = begin + timedelta(minutes=duration)
    local_range = AppointmentLocalRange(
        begin.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")
    )
    return appointment_id, revision, day, local_range


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
    foreign clinic, an actor outside the manager grid, an unknown appointment)
    is the unknown-clinic 404 and changes nothing.
    """
    appointment_id, revision, day, local_range = _inputs(request)
    try:
        authorize_grid_move(clinic_id)
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
            return _render(request, clinic_id, day, notice=(moved, "success"))
        messages.success(request, moved, extra_tags=MOVED_TAG)
        accepted = HttpResponseRedirect(_grid_url(clinic_id, day))
        accepted.status_code = SEE_OTHER
        return accepted
    return _render(
        request, clinic_id, day, notice=(refusal, "warning"), status=CONFLICT_STATUS
    )
