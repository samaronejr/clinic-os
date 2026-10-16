"""Agenda board layout and saved-view codec (todo 24), without a database."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Final, cast
from uuid import UUID, uuid4

from apps.scheduling.agenda_board import (
    AgendaBoard,
    AgendaFilters,
    BoardAppointment,
    BoardCatalog,
    BoardLane,
    DayCount,
    Occupancy,
    ServiceOption,
    _admits,
    _Grant,
)
from apps.scheduling.agenda_board_presenter import (
    action_rows,
    filters_from_session,
    filters_to_session,
    grid_rows,
    list_days,
    month_weeks,
    saved_params,
    saved_spec,
    signature,
)
from apps.scheduling.models import Appointment

DAY: Final = date(2035, 6, 2)


def _item(
    practitioner: UUID,
    start: str,
    end: str,
    *,
    day: date = DAY,
    resources: tuple[UUID, ...] = (),
) -> BoardAppointment:
    return BoardAppointment(
        appointment_id=uuid4(),
        patient_display_name="Sintetico",
        practitioner_id=practitioner,
        resource_ids=resources,
        service_type_id=None,
        start_local=f"{day.isoformat()}T{start}",
        end_local=f"{day.isoformat()}T{end}",
        status="scheduled",
        revision=1,
        movable=True,
        actions=(),
    )


def _board(  # noqa: PLR0913 - a board fixture names every axis it varies
    lanes: tuple[BoardLane, ...],
    appointments: tuple[BoardAppointment, ...] = (),
    occupancy: tuple[Occupancy, ...] = (),
    *,
    view: str = "day",
    days: tuple[date, ...] = (DAY,),
    slots: tuple[str, ...] = ("09:00", "09:30"),
    can_book: bool = True,
    own: UUID | None = None,
    counts: tuple[DayCount, ...] = (),
) -> AgendaBoard:
    return AgendaBoard(
        view=view,
        anchor=days[0],
        first_day=days[0],
        last_day=days[-1],
        timezone_key="America/Sao_Paulo",
        lanes=lanes,
        days=days,
        slots=slots,
        appointments=appointments,
        occupancy=occupancy,
        counts=counts,
        professionals=tuple(lane for lane in lanes if lane.kind == "practitioner"),
        resources=tuple(lane for lane in lanes if lane.kind != "practitioner"),
        services=(),
        filters=AgendaFilters(),
        can_move=True,
        can_book=can_book,
        own_practitioner=own,
        truncated=False,
        subject=lanes[0] if view == "week" and lanes else None,
    )


def test_a_hidden_booking_keeps_its_time_busy_and_unbookable() -> None:
    practitioner = uuid4()
    lane = BoardLane("practitioner", practitioner, "Dra. Sintetica")
    busy = Occupancy(practitioner, f"{DAY}T09:00", f"{DAY}T09:30")
    [first, second] = grid_rows(_board((lane,), occupancy=(busy,)))
    assert (first.cells[0].kind, first.cells[0].free, first.cells[0].bookable) == (
        "continues",
        False,
        False,
    )
    assert (second.cells[0].kind, second.cells[0].bookable) == ("free", True)


def test_another_unit_reads_busy_without_detail_and_is_not_bookable() -> None:
    practitioner = uuid4()
    lane = BoardLane("practitioner", practitioner, "Dra. Sintetica")
    elsewhere = Occupancy(practitioner, f"{DAY}T09:00", f"{DAY}T10:00", other_unit=True)
    rows = grid_rows(_board((lane,), occupancy=(elsewhere,)))
    assert [row.cells[0].kind for row in rows] == ["other_unit", "other_unit"]
    assert not any(row.cells[0].bookable for row in rows)


def test_a_physician_books_only_in_their_own_lane_and_never_in_a_room() -> None:
    own, other, room = uuid4(), uuid4(), uuid4()
    lanes = (
        BoardLane("practitioner", own, "Own"),
        BoardLane("practitioner", other, "Other"),
        BoardLane("room", room, "Sala"),
    )
    [row, _] = grid_rows(_board(lanes, own=own))
    assert [cell.bookable for cell in row.cells] == [True, False, False]
    assert [cell.column for cell in row.cells] == [str(own), str(other), ""]


def test_the_week_view_lays_one_lane_by_day_with_day_columns() -> None:
    practitioner = uuid4()
    lane = BoardLane("practitioner", practitioner, "Dra. Sintetica")
    days = (DAY, date(2035, 6, 3))
    item = _item(practitioner, "09:00", "09:30", day=days[1])
    [first, _] = grid_rows(
        _board(
            (lane,),
            (item,),
            (Occupancy(practitioner, item.start_local, item.end_local),),
            view="week",
            days=days,
        )
    )
    assert [cell.kind for cell in first.cells] == ["free", "booked"]
    assert [cell.column for cell in first.cells] == [day.isoformat() for day in days]
    assert first.cells[1].appointments == (item,)


def test_month_weeks_start_on_monday_and_link_each_day_of_the_month() -> None:
    first, last = date(2035, 6, 1), date(2035, 6, 30)
    days = tuple(date(2035, 6, day) for day in range(1, 31))
    board = replace(
        _board((), view="month", days=days, counts=(DayCount(DAY, 3),)),
        first_day=first,
        last_day=last,
    )
    weeks = month_weeks(uuid4(), board, "2035-06-02")
    assert all(len(week) == 7 for week in weeks)
    assert weeks[0][0].day.weekday() == 0
    cell = next(cell for week in weeks for cell in week if cell.day == DAY)
    assert (cell.total, cell.is_today, cell.in_month) == (3, True, True)
    assert cell.href.endswith(f"/agenda/grid/{DAY.isoformat()}/")
    assert not weeks[0][0].in_month


def test_list_days_group_rows_with_one_fresh_command_each() -> None:
    practitioner = uuid4()
    items = (
        _item(practitioner, "09:00", "09:30"),
        _item(practitioner, "10:00", "10:30"),
        _item(practitioner, "09:00", "09:30", day=date(2035, 6, 3)),
    )
    rows = action_rows(_board((), items, view="list"))
    grouped = list_days(rows)
    assert [day.day for day in grouped] == [DAY, date(2035, 6, 3)]
    assert len({row.command_id for row in rows}) == len(items)


def test_the_signature_changes_with_a_revision_but_not_with_layout() -> None:
    practitioner = uuid4()
    item = _item(practitioner, "09:00", "09:30")
    board = _board((), (item,))
    assert signature(board) == signature(replace(board, slots=("08:00",)))
    moved = replace(item, revision=2)
    assert signature(board) != signature(replace(board, appointments=(moved,)))


def test_session_filters_ignore_malformed_values() -> None:
    professional = uuid4()
    stored = filters_to_session(
        AgendaFilters(professional_id=professional, status="held")
    )
    assert stored == {"professional": str(professional), "status": "held"}
    assert filters_from_session(stored) == AgendaFilters(
        professional_id=professional, status="held"
    )
    assert filters_from_session({"professional": "x", "status": "deleted"}) == (
        AgendaFilters()
    )
    assert filters_from_session("not a mapping") == AgendaFilters()


def test_saved_board_parameters_are_slugs_and_catalog_ids_only() -> None:
    professional = uuid4()
    filters = AgendaFilters(professional_id=professional, status="no_show")
    params = saved_params("list", filters)
    assert params == {
        "view": "grid-list",
        "professional": str(professional),
        "status": "no-show",
    }
    assert saved_spec(params) == ("list", filters)
    assert saved_spec({"view": "week"}) is None
    assert saved_spec({"view": "grid-week", "patient": str(uuid4())}) is None
    assert saved_spec({"view": "grid-week", "status": "unknown"}) is None


def test_the_catalog_admits_only_its_entries_and_drops_retired_ones() -> None:
    professional, room, service = uuid4(), uuid4(), uuid4()
    catalog = BoardCatalog(
        (BoardLane("practitioner", professional, "Dra."),),
        (BoardLane("room", room, "Sala"),),
        (ServiceOption(service, "Consulta", 30),),
    )
    known = AgendaFilters(professional, room, service, "scheduled")
    assert catalog.admits(known)
    assert not catalog.admits(AgendaFilters(professional_id=uuid4()))
    assert catalog.known(AgendaFilters(uuid4(), room, uuid4(), "held")) == (
        AgendaFilters(resource_id=room, status="held")
    )


def test_an_own_schedule_grant_never_matches_a_null_practitioner() -> None:
    actor = uuid4()
    own_only = (_Grant(actor, own=True),)
    unassigned = cast("Appointment", SimpleNamespace(practitioner_id=None))
    assert not _admits(own_only, unassigned)
    assert not _admits(own_only, Appointment(practitioner_id=uuid4()))
    assert _admits(own_only, Appointment(practitioner_id=actor))
    assert _admits((_Grant(actor, own=False),), unassigned)
    assert not _admits((), Appointment(practitioner_id=actor))
