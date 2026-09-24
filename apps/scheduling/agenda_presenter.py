"""Role-scoped agenda presentation bound to Todo 3's civil-date boundaries.

The presenter never computes a UTC bound. It hands Todo 12 the civil view and
date exactly as the route carried them, and only walks whole calendar days or
ISO weeks to build the neighbouring links. Rows are grouped by the clinic-local
civil day they already carry so the week ledger can open one row group per day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from django.urls import reverse
from django.utils import timezone

from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    authorized_view_scope,
)
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import format_local_minute

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scheduling.agenda_queries import AgendaItem, AgendaPage
    from apps.scheduling.booking_queries import BookingPractitioner

DAY_VIEW: Final = "day"
WEEK_VIEW: Final = "week"
STATUS_LABELS: Final = dict(Appointment.Status.choices)
STEP_DAYS: Final = {DAY_VIEW: 1, WEEK_VIEW: 7}
LOCAL_DATE_LENGTH: Final = 10
LOCAL_TIME_SLICE: Final = slice(11, 16)


@dataclass(frozen=True, slots=True)
class AgendaScreen:
    """Bind one authorized agenda to its timezone and role-appropriate controls."""

    timezone_key: str
    can_manage: bool


@dataclass(frozen=True, slots=True)
class AgendaRow:
    """Bind one agenda item to its same-object transition routes."""

    item: AgendaItem
    status_label: str
    reschedule_url: str
    cancel_url: str
    is_scheduled: bool
    start_time: str
    end_time: str


@dataclass(frozen=True, slots=True)
class AgendaDay:
    """One clinic-local civil day and its rows in start order."""

    date: date
    rows: tuple[AgendaRow, ...]


@dataclass(frozen=True, slots=True)
class BookingWindowRow:
    """One promised window as the booking ledger shows it: who, which day, when."""

    practitioner_label: str
    date: date
    start_local: str
    end_local: str
    start_time: str
    end_time: str


def agenda_screen(clinic_id: UUID) -> AgendaScreen:
    """Authorize the clinic once and derive manager-versus-physician controls."""
    scope = authorized_view_scope(clinic_id)
    timezone_key = scope.clinic.timezone
    if not isinstance(timezone_key, str) or not timezone_key:
        raise AvailabilityAccessDeniedError
    return AgendaScreen(
        timezone_key=timezone_key,
        can_manage=scope.practitioner_id is None,
    )


def clinic_local_today(timezone_key: str) -> str:
    """Return the current clinic-local civil date without a naive bound."""
    now = timezone.now().replace(second=0, microsecond=0)
    return format_local_minute(now, timezone_key)[:LOCAL_DATE_LENGTH]


def agenda_url(clinic_id: UUID, view: str, day: str, page: int) -> str:
    """Return the state-free agenda path for one civil view, date, and page."""
    return reverse("scheduling:agenda-at", args=(clinic_id, view, day, page))


def presented_rows(page: AgendaPage, *, can_manage: bool) -> tuple[AgendaRow, ...]:
    """Bind every visible appointment to its transition routes when allowed."""
    return tuple(
        AgendaRow(
            item=item,
            status_label=STATUS_LABELS.get(item.status, item.status),
            reschedule_url=(
                reverse(
                    "scheduling:appointment-reschedule", args=(item.appointment_id,)
                )
                if can_manage
                else ""
            ),
            cancel_url=(
                reverse("scheduling:appointment-cancel", args=(item.appointment_id,))
                if can_manage
                else ""
            ),
            is_scheduled=item.status == Appointment.Status.SCHEDULED,
            start_time=item.start_local[LOCAL_TIME_SLICE],
            end_time=item.end_local[LOCAL_TIME_SLICE],
        )
        for item in page.items
    )


def presented_days(page: AgendaPage, *, can_manage: bool) -> tuple[AgendaDay, ...]:
    """Group the page's rows by the clinic-local civil day they start on."""
    days: list[AgendaDay] = []
    for row in presented_rows(page, can_manage=can_manage):
        civil = date.fromisoformat(row.item.start_local[:LOCAL_DATE_LENGTH])
        if days and days[-1].date == civil:
            days[-1] = AgendaDay(civil, (*days[-1].rows, row))
        else:
            days.append(AgendaDay(civil, (row,)))
    return tuple(days)


def booking_window_rows(
    practitioners: tuple[BookingPractitioner, ...],
) -> tuple[BookingWindowRow, ...]:
    """Flatten promised windows by physician, then clinic-local day and start."""
    return tuple(
        BookingWindowRow(
            practitioner_label=practitioner.display_identifier,
            date=date.fromisoformat(window.start_local[:LOCAL_DATE_LENGTH]),
            start_local=window.start_local,
            end_local=window.end_local,
            start_time=window.start_local[LOCAL_TIME_SLICE],
            end_time=window.end_local[LOCAL_TIME_SLICE],
        )
        for practitioner in practitioners
        for window in practitioner.windows
    )


def _shifted(day: date, view: str, steps: int) -> str:
    return (day + timedelta(days=STEP_DAYS[view] * steps)).isoformat()


def navigation(
    clinic_id: UUID,
    page: AgendaPage,
    *,
    today: str,
) -> dict[str, object]:
    """Return the neighbouring civil-date, view, today and page links."""
    day = page.date
    current = page.view
    other = WEEK_VIEW if current == DAY_VIEW else DAY_VIEW
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=6)
    today_date = date.fromisoformat(today)
    links: dict[str, object] = {
        "previous_url": agenda_url(clinic_id, current, _shifted(day, current, -1), 1),
        "next_url": agenda_url(clinic_id, current, _shifted(day, current, 1), 1),
        "today_url": agenda_url(clinic_id, current, today, 1),
        "other_view": other,
        "other_view_url": agenda_url(clinic_id, other, day.isoformat(), 1),
        "day_view_url": agenda_url(clinic_id, DAY_VIEW, day.isoformat(), 1),
        "week_view_url": agenda_url(clinic_id, WEEK_VIEW, day.isoformat(), 1),
        "week_start": week_start,
        "week_end": week_end,
        "is_today": (
            day == today_date
            if current == DAY_VIEW
            else week_start <= today_date <= week_end
        ),
        "patient_list_url": reverse("intake:patient-list", args=(clinic_id,)),
    }
    if page.page > 1:
        links["previous_page_url"] = agenda_url(
            clinic_id, current, day.isoformat(), page.page - 1
        )
    if page.page < page.page_count:
        links["next_page_url"] = agenda_url(
            clinic_id, current, day.isoformat(), page.page + 1
        )
    return links
