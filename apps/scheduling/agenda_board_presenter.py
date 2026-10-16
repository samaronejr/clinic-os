"""Lay an ``AgendaBoard`` out for the templates; format only, never authorize.

Cells, month weeks and list days are derived from the board the service
returned. URLs carry only the clinic, the civil view and the civil date.
Filters live in the server-side session and saved views; their parameters are
the clinic catalog identifiers and closed slugs ``identity.saved_views`` admits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.urls import reverse
from django.utils.translation import gettext, gettext_noop

from apps.identity.templatetags.clinic_locale import appointment_label
from apps.scheduling.agenda_board import (
    BOARD_VIEWS,
    AgendaBoard,
    AgendaFilters,
    BoardAppointment,
    BoardLane,
    Occupancy,
    minute_of,
)
from apps.scheduling.agenda_grid import SLOT_MINUTES
from apps.scheduling.agenda_queries import AGENDA_STATUSES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.identity.saved_views import SavedViewRecord

SESSION_PREFIX: Final = "scheduling.agenda.board."
SAVED_DESTINATION: Final = "agenda"
SAVED_VIEW_PREFIX: Final = "grid-"
BOOKING_DURATIONS: Final = (15, 30, 45, 60, 90, 120)
DEFAULT_DURATION: Final = 30
VIEW_LABELS: Final = {
    "day": gettext_noop("Day"),
    "week": gettext_noop("Week"),
    "month": gettext_noop("Month"),
    "list": gettext_noop("List"),
}
LANE_KIND_LABELS: Final = {
    "room": gettext_noop("Room"),
    "equipment": gettext_noop("Equipment"),
}
_FILTER_KEYS: Final = ("professional", "resource", "service", "status")


@dataclass(frozen=True, slots=True)
class BoardCell:
    """One (lane or day) x half-hour cell of the day or week grid."""

    kind: str
    day: str
    slot: str
    lane: BoardLane
    column: str
    appointments: tuple[BoardAppointment, ...]
    free: bool
    bookable: bool


@dataclass(frozen=True, slots=True)
class GridRow:
    """One half-hour row of the day or week grid."""

    time: str
    cells: tuple[BoardCell, ...]


@dataclass(frozen=True, slots=True)
class MonthDay:
    """One calendar cell of the month view."""

    day: date
    in_month: bool
    total: int
    href: str
    is_today: bool


@dataclass(frozen=True, slots=True)
class ActionRow:
    """An appointment with the fresh command id its native action form posts."""

    item: BoardAppointment
    command_id: UUID


@dataclass(frozen=True, slots=True)
class ListDay:
    """One clinic-local date of the list view with its appointments."""

    day: date
    rows: tuple[ActionRow, ...]


@dataclass(frozen=True, slots=True)
class SavedBoardView:
    """One saved board view of the current user, named from its parameters."""

    view_id: UUID
    label: str


def session_key(clinic_id: UUID) -> str:
    """Return the session key holding one clinic's board filters."""
    return f"{SESSION_PREFIX}{clinic_id}"


def board_url(clinic_id: UUID, view: str, day: date) -> str:
    """Return the state-free board path; the day view keeps the grid URL."""
    if view == "day":
        return reverse("scheduling:agenda-grid", args=(clinic_id, day.isoformat()))
    return reverse("scheduling:agenda-board", args=(clinic_id, view, day.isoformat()))


def _shift(view: str, anchor: date, steps: int) -> date:
    if view == "day":
        return anchor + timedelta(days=steps)
    if view == "month":
        month = anchor.month - 1 + steps
        return date(anchor.year + month // 12, month % 12 + 1, 1)
    return anchor + timedelta(days=7 * steps)


def navigation(clinic_id: UUID, board: AgendaBoard, today: str) -> dict[str, object]:
    """Previous/next/today steps and the view switch, all state-free links."""
    today_date = date.fromisoformat(today)
    return {
        "previous_url": board_url(
            clinic_id, board.view, _shift(board.view, board.anchor, -1)
        ),
        "next_url": board_url(
            clinic_id, board.view, _shift(board.view, board.anchor, 1)
        ),
        "today_url": board_url(clinic_id, board.view, today_date),
        "is_today": board.first_day <= today_date <= board.last_day,
        "view_links": [
            {
                "key": key,
                "label": gettext(VIEW_LABELS[key]),
                "href": board_url(clinic_id, key, board.anchor),
                "current": key == board.view,
            }
            for key in BOARD_VIEWS
        ],
        "ledger_url": reverse(
            "scheduling:agenda-at",
            args=(
                clinic_id,
                "day" if board.view == "day" else "week",
                board.anchor.isoformat(),
                1,
            ),
        ),
    }


def _overlaps(item: Occupancy, start: str, end: str) -> bool:
    return item.start_local < end and start < item.end_local


def _slot_end(day: str, slot: str) -> str:
    minute = minute_of(slot) + SLOT_MINUTES
    if minute >= 24 * 60:
        following = date.fromisoformat(day) + timedelta(days=1)
        return f"{following.isoformat()}T00:00"
    return f"{day}T{minute // 60:02d}:{minute % 60:02d}"


def _cell(  # noqa: PLR0913 - one cell needs its lane, time and indexes
    board: AgendaBoard,
    lane: BoardLane,
    day: str,
    slot: str,
    *,
    column: str,
    starting: Mapping[tuple[UUID, str, str], tuple[BoardAppointment, ...]],
    occupied: Mapping[UUID, tuple[Occupancy, ...]],
) -> BoardCell:
    start = f"{day}T{slot}"
    end = _slot_end(day, slot)
    here = [
        item for item in occupied.get(lane.lane_id, ()) if _overlaps(item, start, end)
    ]
    clinic_busy = any(not item.other_unit for item in here)
    appointments = starting.get((lane.lane_id, day, slot), ())
    if appointments:
        kind = "booked"
    elif clinic_busy:
        kind = "continues"
    elif here:
        kind = "other_unit"
    else:
        kind = "free"
    own = board.own_practitioner
    bookable = (
        not here
        and board.can_book
        and (
            own is None if lane.kind != "practitioner" else own in {None, lane.lane_id}
        )
    )
    return BoardCell(
        kind=kind,
        day=day,
        slot=slot,
        lane=lane,
        column=column,
        appointments=appointments,
        free=not here,
        bookable=bookable,
    )


def _indexes(
    board: AgendaBoard,
) -> tuple[
    dict[tuple[UUID, str, str], tuple[BoardAppointment, ...]],
    dict[UUID, tuple[Occupancy, ...]],
]:
    starting: dict[tuple[UUID, str, str], list[BoardAppointment]] = {}
    for item in board.appointments:
        minute = minute_of(item.start_time)
        slot = minute - minute % SLOT_MINUTES
        key_slot = f"{slot // 60:02d}:{slot % 60:02d}"
        for lane in board.lanes:
            if item.in_lane(lane):
                starting.setdefault((lane.lane_id, item.day, key_slot), []).append(item)
    occupied: dict[UUID, list[Occupancy]] = {}
    for busy in board.occupancy:
        occupied.setdefault(busy.lane_id, []).append(busy)
    return (
        {key: tuple(value) for key, value in starting.items()},
        {key: tuple(value) for key, value in occupied.items()},
    )


def grid_rows(board: AgendaBoard) -> list[GridRow]:
    """Day view: lanes as columns; week view: the subject's days as columns."""
    starting, occupied = _indexes(board)
    rows: list[GridRow] = []
    for slot in board.slots:
        if board.view == "day":
            day = board.anchor.isoformat()
            cells = [
                _cell(
                    board,
                    lane,
                    day,
                    slot,
                    column=str(lane.lane_id) if lane.kind == "practitioner" else "",
                    starting=starting,
                    occupied=occupied,
                )
                for lane in board.lanes
            ]
        elif board.subject is not None:
            subject = board.subject
            cells = [
                _cell(
                    board,
                    subject,
                    day.isoformat(),
                    slot,
                    column=day.isoformat() if subject.kind == "practitioner" else "",
                    starting=starting,
                    occupied=occupied,
                )
                for day in board.days
            ]
        else:
            cells = []
        rows.append(GridRow(slot, tuple(cells)))
    return rows


def month_weeks(
    clinic_id: UUID, board: AgendaBoard, today: str
) -> list[list[MonthDay]]:
    """Monday-first calendar weeks covering the month, each day with its count."""
    totals = {count.day: count.total for count in board.counts}
    start = board.first_day - timedelta(days=board.first_day.weekday())
    end = board.last_day + timedelta(days=6 - board.last_day.weekday())
    today_date = date.fromisoformat(today)
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    cells = [
        MonthDay(
            day=day,
            in_month=board.first_day <= day <= board.last_day,
            total=totals.get(day, 0),
            href=board_url(clinic_id, "day", day),
            is_today=day == today_date,
        )
        for day in days
    ]
    return [cells[index : index + 7] for index in range(0, len(cells), 7)]


def action_rows(board: AgendaBoard) -> list[ActionRow]:
    """Pair each appointment with a new command id (a replay key per render)."""
    return [ActionRow(item, uuid4()) for item in board.appointments]


def list_days(rows: list[ActionRow]) -> list[ListDay]:
    """Group the list view's appointments by clinic-local date, in order."""
    grouped: dict[str, list[ActionRow]] = {}
    for row in rows:
        grouped.setdefault(row.item.day, []).append(row)
    return [
        ListDay(date.fromisoformat(day), tuple(rows)) for day, rows in grouped.items()
    ]


def lane_labels(board: AgendaBoard) -> dict[UUID, str]:
    """Name every professional and resource lane for meta lines."""
    return {
        lane.lane_id: lane.label for lane in (*board.professionals, *board.resources)
    }


def signature(board: AgendaBoard) -> str:
    """Return an opaque digest of what the board shows, to detect remote changes."""
    digest = hashlib.sha256()
    for item in board.appointments:
        digest.update(f"{item.appointment_id}:{item.revision}:{item.status};".encode())
    for busy in board.occupancy:
        digest.update(f"{busy.lane_id}:{busy.start_local}:{busy.end_local};".encode())
    for count in board.counts:
        digest.update(f"{count.day}:{count.total};".encode())
    return digest.hexdigest()[:16]


def filters_from_session(raw: object) -> AgendaFilters:
    """Parse the stored filters; anything malformed is simply no filter."""
    if not isinstance(raw, dict):
        return AgendaFilters()

    def identifier(key: str) -> UUID | None:
        value = raw.get(key)
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    status = raw.get("status")
    return AgendaFilters(
        professional_id=identifier("professional"),
        resource_id=identifier("resource"),
        service_type_id=identifier("service"),
        status=status
        if isinstance(status, str) and status in AGENDA_STATUSES
        else None,
    )


def filters_to_session(filters: AgendaFilters) -> dict[str, str]:
    """Serialize filters for the session (catalog identifiers and a status)."""
    stored = {
        "professional": filters.professional_id,
        "resource": filters.resource_id,
        "service": filters.service_type_id,
    }
    result = {key: str(value) for key, value in stored.items() if value is not None}
    if filters.status is not None:
        result["status"] = filters.status
    return result


def saved_params(view: str, filters: AgendaFilters) -> dict[str, str]:
    """Encode a board view for ``identity.saved_views`` (slugs, catalog ids)."""
    params = {"view": SAVED_VIEW_PREFIX + view}
    stored = filters_to_session(filters)
    if "status" in stored:
        stored["status"] = stored["status"].replace("_", "-")
    return params | stored


def saved_spec(params: Mapping[str, str]) -> tuple[str, AgendaFilters] | None:
    """Decode a saved board view, or ``None`` for any other saved view."""
    raw_view = params.get("view", "")
    view = raw_view.removeprefix(SAVED_VIEW_PREFIX)
    if raw_view == view or view not in BOARD_VIEWS:
        return None
    if set(params) - {"view", *_FILTER_KEYS}:
        return None
    status = params.get("status")
    decoded = dict(params)
    if status is not None:
        decoded["status"] = status.replace("-", "_")
    filters = filters_from_session(decoded)
    if status is not None and filters.status is None:
        return None
    return view, filters


def saved_views(
    records: tuple[SavedViewRecord, ...], board: AgendaBoard
) -> list[SavedBoardView]:
    """Name the user's saved board views: view, then each filter's label."""
    labels = lane_labels(board)
    services = {option.service_type_id: option.label for option in board.services}
    result: list[SavedBoardView] = []
    for record in records:
        spec = (
            saved_spec(record.params)
            if record.destination == SAVED_DESTINATION
            else None
        )
        if spec is None:
            continue
        view, filters = spec
        parts = [gettext(VIEW_LABELS[view])]
        parts.extend(
            labels.get(value, gettext("Unavailable"))
            for value in (filters.professional_id, filters.resource_id)
            if value is not None
        )
        if filters.service_type_id is not None:
            parts.append(services.get(filters.service_type_id, gettext("Unavailable")))
        if filters.status is not None:
            parts.append(appointment_label(filters.status))
        result.append(SavedBoardView(record.id, " · ".join(parts)))
    return result


__all__ = (
    "BOOKING_DURATIONS",
    "DEFAULT_DURATION",
    "LANE_KIND_LABELS",
    "ActionRow",
    "BoardCell",
    "GridRow",
    "ListDay",
    "MonthDay",
    "SavedBoardView",
    "action_rows",
    "board_url",
    "filters_from_session",
    "filters_to_session",
    "grid_rows",
    "lane_labels",
    "list_days",
    "month_weeks",
    "navigation",
    "saved_params",
    "saved_spec",
    "saved_views",
    "session_key",
    "signature",
)
