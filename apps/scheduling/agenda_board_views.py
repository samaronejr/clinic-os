"""Agenda board rendering, preferences and actions (HTMX over native forms).

Board URLs carry only the clinic, the civil view and the civil date. Filters
are kept in the server-side session, saved views in ``identity.saved_views``,
and every record selector (appointment, revision, command, booking key) only
in POST bodies. Refusals share the unknown-clinic 404 and write nothing, not
even the session; a booking or transition that loses a race answers 409 with
the current board and a worded conflict.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseBase, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.core.patient_context import current_patient
from apps.identity.otp import privileged_totp_required
from apps.identity.saved_views import (
    SavedViewError,
    archive_saved_view,
    list_saved_views,
    save_view,
)
from apps.scheduling.agenda_board import (
    BOARD_VIEWS,
    AgendaFilters,
    board_catalog,
    free_move_slots,
    view_agenda_board,
)
from apps.scheduling.agenda_board_presenter import (
    BOOKING_DURATIONS,
    DEFAULT_DURATION,
    LANE_KIND_LABELS,
    SAVED_DESTINATION,
    action_rows,
    board_url,
    filters_from_session,
    filters_to_session,
    grid_rows,
    lane_labels,
    list_days,
    month_weeks,
    navigation,
    saved_params,
    saved_spec,
    saved_views,
    session_key,
    signature,
)
from apps.scheduling.agenda_grid import authorize_grid_move
from apps.scheduling.agenda_presenter import clinic_local_today
from apps.scheduling.agenda_queries import AGENDA_STATUSES
from apps.scheduling.appointment_forms import (
    BOOKING_PRACTITIONER_MESSAGE,
    INVALID_BOOKING_MESSAGE,
    WINDOW_MESSAGE,
)
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.lifecycle_views import IDEMPOTENCY_MESSAGE
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import (
    AgendaInputError,
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
    AppointmentPractitionerError,
    AvailabilityAccessDeniedError,
    ServiceBooking,
    SlotConflict,
    arrive,
    book,
    cancel,
    create_hold,
    create_service_hold,
    mark_no_show,
    view_appointment_for_transition,
)
from apps.scheduling.timezones import format_local_minute

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.utils.functional import Promise

    from apps.scheduling.agenda_board import AgendaBoard
    from apps.scheduling.appointment_view import AppointmentTransitionView

PAGE_TEMPLATE: Final = "scheduling/agenda_grid.html"
PARTIAL_TEMPLATE: Final = "scheduling/partials/agenda_grid.html"
OPTIONS_TEMPLATE: Final = "scheduling/partials/agenda_move_options.html"
MOVE_TEMPLATE: Final = "scheduling/agenda_move.html"
CONFLICT_STATUS: Final = 409
SEE_OTHER: Final = 303
OUTCOME_TAG: Final = "scheduling.board.outcome"
MAX_DURATION: Final = 720
CLOCK: Final = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
PREFERENCE_FIELDS: Final = frozenset(
    {
        "csrfmiddlewaretoken",
        "intent",
        "unit",
        "professional",
        "resource",
        "service",
        "status",
        "saved_view",
    }
)
PREFERENCE_INTENTS: Final = frozenset(
    {"filter", "clear", "save", "open", "archive", "unit"}
)
ACTION_FIELDS: Final = frozenset(
    {
        "csrfmiddlewaretoken",
        "intent",
        "view",
        "day",
        "appointment_id",
        "expected_revision",
        "command_id",
        "practitioner",
        "start",
        "duration",
        "service",
        "resources",
        "idempotency_key",
    }
)
TRANSITIONS: Final = frozenset({"confirm", "cancel", "arrive", "no_show"})
TAKEN_MESSAGE: Final = _("This time was just taken. Choose another time.")
NO_PATIENT_MESSAGE: Final = _(
    "Choose the patient first: search for them with Search (Ctrl K), then "
    "come back to this time."
)
SAVE_FAILED_MESSAGE: Final = _(
    "This view could not be saved. Remove a saved view you no longer use."
)
SAVED_MESSAGE: Final = _("View saved. Open it again from Saved views.")
HELD_MESSAGE: Final = _(
    "Time reserved until %(time)s. Confirm it to book the appointment."
)
OUTCOMES: Final[dict[str, Promise]] = {
    "confirm": _("Appointment booked."),
    "cancel": _("Appointment cancelled. The time is free again."),
    "arrive": _("Arrival recorded."),
    "no_show": _("No-show recorded."),
}
REFUSED_TITLE: Final = _("Appointment not changed")


def board_continuation(clinic_id: UUID) -> str:
    """Resume a board action challenge at the clinic agenda, never replaying it."""
    return reverse("scheduling:agenda", args=(clinic_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _civil_day(raw: object) -> date:
    try:
        parsed = date.fromisoformat(str(raw))
    except ValueError as error:
        raise Http404 from error
    if parsed.isoformat() != raw:
        raise Http404
    return parsed


def _required_uuid(raw: object) -> UUID:
    try:
        return UUID(str(raw))
    except ValueError as error:
        raise Http404 from error


def _optional_uuid(raw: object) -> UUID | None:
    return None if raw in {None, ""} else _required_uuid(raw)


def _board_filters(request: HttpRequest, clinic_id: UUID) -> AgendaFilters:
    return filters_from_session(request.session.get(session_key(clinic_id)))


def _read_board(
    request: HttpRequest, clinic_id: UUID, view: str, day: str
) -> AgendaBoard:
    filters = _board_filters(request, clinic_id)
    try:
        try:
            return view_agenda_board(
                clinic_id=clinic_id, view=view, date=day, filters=filters
            )
        except AgendaInputError:
            if filters == AgendaFilters():
                raise
            # A stored filter can outlive its catalog entry; show the board
            # unfiltered rather than refuse it, without touching the session.
            return view_agenda_board(clinic_id=clinic_id, view=view, date=day)
    except (AvailabilityAccessDeniedError, AgendaInputError) as error:
        raise Http404 from error


def render_board(  # noqa: PLR0913 - one render needs its full response context
    request: HttpRequest,
    clinic_id: UUID,
    view: str,
    day: str,
    *,
    notice: tuple[str, str] = ("", ""),
    status: int = 200,
) -> HttpResponseBase:
    """Render the board page, or only its refetchable region for HTMX."""
    if view not in BOARD_VIEWS:
        raise Http404
    board = _read_board(request, clinic_id, view, day)
    today = clinic_local_today(board.timezone_key)
    partial = _is_htmx(request)
    rows = action_rows(board)
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "board": board,
        "grid": board,
        "names": lane_labels(board),
        "lane_kinds": {key: str(label) for key, label in LANE_KIND_LABELS.items()},
        "rows": grid_rows(board) if view in {"day", "week"} else [],
        "weeks": month_weeks(clinic_id, board, today) if view == "month" else [],
        "list_days": list_days(rows) if view == "list" else [],
        "holds": [row for row in rows if "confirm" in row.item.actions],
        "month_total": sum(count.total for count in board.counts),
        "signature": signature(board),
        "grid_url": board_url(clinic_id, view, board.anchor),
        "move_url": reverse("scheduling:agenda-grid-move", args=(clinic_id,)),
        "action_url": reverse("scheduling:agenda-grid-action", args=(clinic_id,)),
        "realtime_enabled": settings.REALTIME_ENABLED,
        "realtime_polling": settings.REALTIME_POLLING_FALLBACK,
        "notice": notice[0],
        "notice_tone": notice[1],
        "refused_title": REFUSED_TITLE,
        **navigation(clinic_id, board, today),
    }
    if not partial:
        context.update(
            {
                "saved": saved_views(list_saved_views(clinic_id=clinic_id), board),
                "status_options": AGENDA_STATUSES,
                "durations": BOOKING_DURATIONS,
                "default_duration": DEFAULT_DURATION,
                "booking_key": uuid4(),
                "registry_url": reverse("intake:patient-list", args=(clinic_id,)),
            }
        )
    template = PARTIAL_TEMPLATE if partial else PAGE_TEMPLATE
    return render(request, template, context, status=status)


def _see_other(target: str) -> HttpResponseRedirect:
    redirect = HttpResponseRedirect(target)
    redirect.status_code = SEE_OTHER
    return redirect


def _known_filters(request: HttpRequest, clinic_id: UUID) -> AgendaFilters:
    """Parse posted filters; one naming anything outside the catalog is a 404."""
    status = request.POST.get("status", "")
    if status and status not in AGENDA_STATUSES:
        raise Http404
    filters = AgendaFilters(
        professional_id=_optional_uuid(request.POST.get("professional")),
        resource_id=_optional_uuid(request.POST.get("resource")),
        service_type_id=_optional_uuid(request.POST.get("service")),
        status=status or None,
    )
    if not board_catalog(clinic_id=clinic_id).admits(filters):
        raise Http404
    return filters


def _opened_view(clinic_id: UUID, raw: object) -> tuple[str, AgendaFilters]:
    view_id = _required_uuid(raw)
    record = next(
        (row for row in list_saved_views(clinic_id=clinic_id) if row.id == view_id),
        None,
    )
    spec = (
        saved_spec(record.params)
        if record is not None and record.destination == SAVED_DESTINATION
        else None
    )
    if spec is None:
        raise Http404
    view, filters = spec
    return view, board_catalog(clinic_id=clinic_id).known(filters)


def _save_current(request: HttpRequest, clinic_id: UUID, view: str) -> None:
    filters = board_catalog(clinic_id=clinic_id).known(
        _board_filters(request, clinic_id)
    )
    try:
        save_view(
            clinic_id=clinic_id,
            destination=SAVED_DESTINATION,
            params=saved_params(view, filters),
        )
    except SavedViewError:
        messages.warning(request, str(SAVE_FAILED_MESSAGE), extra_tags=OUTCOME_TAG)
    else:
        messages.success(request, str(SAVED_MESSAGE), extra_tags=OUTCOME_TAG)


def apply_board_preferences(
    request: HttpRequest, clinic_id: UUID, view: str, day: str
) -> HttpResponseBase:
    """Change the board's filters, unit or saved views, then show it again (PRG).

    The clinic is authorized and every posted value checked before the first
    session or saved-view write, so a refusal changes nothing.
    """
    if set(request.POST) - PREFERENCE_FIELDS or view not in BOARD_VIEWS:
        raise Http404
    anchor = _civil_day(day)
    intent = request.POST.get("intent", "")
    if intent not in PREFERENCE_INTENTS:
        raise Http404
    try:
        board_catalog(clinic_id=clinic_id)
        if intent == "unit":
            unit = _required_uuid(request.POST.get("unit"))
            return _see_other(board_url(unit, view, anchor))
        if intent == "archive":
            archive_saved_view(
                clinic_id=clinic_id,
                view_id=_required_uuid(request.POST.get("saved_view")),
            )
            return _see_other(board_url(clinic_id, view, anchor))
        if intent == "save":
            _save_current(request, clinic_id, view)
            return _see_other(board_url(clinic_id, view, anchor))
        if intent == "open":
            view, filters = _opened_view(clinic_id, request.POST.get("saved_view"))
        elif intent == "filter":
            filters = _known_filters(request, clinic_id)
        else:
            filters = AgendaFilters()
    except (AvailabilityAccessDeniedError, SavedViewError) as error:
        raise Http404 from error
    stored = filters_to_session(filters)
    key = session_key(clinic_id)
    if stored:
        request.session[key] = stored
    else:
        request.session.pop(key, None)
    return _see_other(board_url(clinic_id, view, anchor))


def _transition(intent: str, request: HttpRequest, clinic_id: UUID) -> str:
    """Apply one lifecycle edge; explicit dispatch keeps each service reachable."""
    appointment_id = _required_uuid(request.POST.get("appointment_id"))
    command_id = _required_uuid(request.POST.get("command_id"))
    try:
        revision = int(request.POST.get("expected_revision", ""))
    except ValueError as error:
        raise Http404 from error
    if intent == "confirm":
        book(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            expected_revision=revision,
            command_id=command_id,
        )
    elif intent == "cancel":
        cancel(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            expected_revision=revision,
            command_id=command_id,
        )
    elif intent == "arrive":
        arrive(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            expected_revision=revision,
            command_id=command_id,
        )
    else:
        mark_no_show(
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            expected_revision=revision,
            command_id=command_id,
        )
    return str(OUTCOMES[intent])


def _booking_range(
    request: HttpRequest, day: date, service_minutes: int | None
) -> AppointmentLocalRange:
    start = request.POST.get("start", "")
    try:
        duration = int(request.POST.get("duration", ""))
    except ValueError as error:
        raise Http404 from error
    if CLOCK.fullmatch(start) is None or not 1 <= duration <= MAX_DURATION:
        raise Http404
    begin = datetime.fromisoformat(f"{day.isoformat()}T{start}")
    end = begin + timedelta(minutes=service_minutes or duration)
    return AppointmentLocalRange(
        begin.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")
    )


def _hold(request: HttpRequest, clinic_id: UUID, day: date) -> str | None:
    """Reserve a time for the patient in context; ``None`` when no one is."""
    practitioner_id = _required_uuid(request.POST.get("practitioner"))
    idempotency_key = _required_uuid(request.POST.get("idempotency_key"))
    service_id = _optional_uuid(request.POST.get("service"))
    resource_ids = tuple(
        _required_uuid(raw) for raw in request.POST.getlist("resources")
    )
    if service_id is None and resource_ids:
        raise Http404
    # A service books its own duration; an unknown one is refused by the hold.
    service_minutes = next(
        (
            option.duration_minutes
            for option in board_catalog(clinic_id=clinic_id).services
            if option.service_type_id == service_id
        ),
        None,
    )
    local_range = _booking_range(request, day, service_minutes)
    enrollment_id = current_patient(request, clinic_id)
    if enrollment_id is None:
        return None
    if service_id is None:
        held = create_hold(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            practitioner_id=practitioner_id,
            local_range=local_range,
            idempotency_key=idempotency_key,
        )
    else:
        held = create_service_hold(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            practitioner_id=practitioner_id,
            booking=ServiceBooking(local_range, service_id, resource_ids),
            idempotency_key=idempotency_key,
        )
    deadline = held.hold_expires_at
    zone = held.clinic.timezone
    until = (
        format_local_minute(deadline.replace(second=0, microsecond=0), zone)[11:16]
        if deadline is not None and isinstance(zone, str)
        else ""
    )
    return str(HELD_MESSAGE % {"time": until})


def _refusal(error: Exception) -> str:
    if isinstance(error, SchedulingRuleError):
        return str(
            TAKEN_MESSAGE if error.code == "resource_conflict" else error.message
        )
    if isinstance(error, AppointmentLifecycleError):
        return str(error.message)
    messages_by_type: dict[type[Exception], Promise] = {
        SlotConflict: TAKEN_MESSAGE,
        AppointmentAvailabilityError: WINDOW_MESSAGE,
        AppointmentPractitionerError: BOOKING_PRACTITIONER_MESSAGE,
        AppointmentIdempotencyConflictError: IDEMPOTENCY_MESSAGE,
        AppointmentCreateInputError: INVALID_BOOKING_MESSAGE,
    }
    return str(
        next(text for kind, text in messages_by_type.items() if isinstance(error, kind))
    )


REFUSED: Final = (
    SchedulingRuleError,
    AppointmentLifecycleError,
    SlotConflict,
    AppointmentAvailabilityError,
    AppointmentPractitionerError,
    AppointmentIdempotencyConflictError,
    AppointmentCreateInputError,
)


def _find(
    request: HttpRequest, clinic_id: UUID, view: str, day: date
) -> HttpResponseBase:
    """Answer free start times: options for the dialog, or the native move form."""
    appointment_id = _required_uuid(request.POST.get("appointment_id"))
    htmx = _is_htmx(request)
    revision = duration = 0
    if not htmx:
        try:
            revision = int(request.POST.get("expected_revision", ""))
            duration = int(request.POST.get("duration", ""))
        except ValueError as error:
            raise Http404 from error
        if not 1 <= duration <= MAX_DURATION:
            raise Http404
    found = free_move_slots(
        clinic_id=clinic_id, appointment_id=appointment_id, date=day.isoformat()
    )
    if htmx:
        return render(request, OPTIONS_TEMPLATE, {"found": found})
    current: AppointmentTransitionView = view_appointment_for_transition(
        appointment_id=appointment_id
    )
    return render(
        request,
        MOVE_TEMPLATE,
        {
            "found": found,
            "view": view,
            "appointment": current,
            "appointment_id": appointment_id,
            "expected_revision": revision,
            "day": day.isoformat(),
            "start": found.starts[0] if found.starts else "",
            "duration": duration,
            "move_url": reverse("scheduling:agenda-grid-move", args=(clinic_id,)),
            "action_url": reverse("scheduling:agenda-grid-action", args=(clinic_id,)),
            "grid_url": board_url(clinic_id, view, day),
        },
    )


@privileged_totp_required(board_continuation)
@require_http_methods(["POST"])
def agenda_board_action_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Book (hold, confirm), cancel, record arrival or no-show, or find free times.

    The board's scope is authorized before any write; each service then
    re-decides its own terms under its locks. Unknown, foreign and unauthorized
    selectors share one 404 and write nothing; a lost race answers 409 with the
    current board.
    """
    if set(request.POST) - ACTION_FIELDS:
        raise Http404
    intent = request.POST.get("intent", "")
    view = request.POST.get("view", "day")
    if intent not in {*TRANSITIONS, "hold", "find"} or view not in BOARD_VIEWS:
        raise Http404
    day = _civil_day(request.POST.get("day"))
    try:
        authorize_grid_move(clinic_id)
        if intent == "find":
            return _find(request, clinic_id, view, day)
        if intent == "hold":
            outcome = _hold(request, clinic_id, day)
            if outcome is None:
                return render_board(
                    request,
                    clinic_id,
                    view,
                    day.isoformat(),
                    notice=(str(NO_PATIENT_MESSAGE), "warning"),
                    status=CONFLICT_STATUS,
                )
        else:
            outcome = _transition(intent, request, clinic_id)
    except (
        AppointmentAccessDeniedError,
        AvailabilityAccessDeniedError,
        AppointmentLifecycleInputError,
    ) as error:
        raise Http404 from error
    except REFUSED as error:
        return render_board(
            request,
            clinic_id,
            view,
            day.isoformat(),
            notice=(_refusal(error), "warning"),
            status=CONFLICT_STATUS,
        )
    if _is_htmx(request):
        return render_board(
            request, clinic_id, view, day.isoformat(), notice=(outcome, "success")
        )
    messages.success(request, outcome, extra_tags=OUTCOME_TAG)
    return _see_other(board_url(clinic_id, view, day))
