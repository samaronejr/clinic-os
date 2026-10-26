"""Clinician Today: the actor's own clinic-local day, arrivals and inbox counts.

Only appointments the physician is the practitioner of are read, under the
``appointment.read_own`` permission; nothing here widens the agenda's
own-appointment scope. Encounters are looked up only for those same-clinic
appointments. Inbox counts appear only when the inbox itself would open. The
pre-visit brief slot is reserved for todo 44 and is reported as unavailable
while no AI capability exists. Committed appointment writes publish the clinic
inbox hint Today subscribes to, so a connected board refetches its own day
without a clinic-wide agenda grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from django.db.models.signals import post_save
from django.utils import timezone

from apps.ehr.models import Encounter
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import civil_day_bounds
from apps.workflows.inbox import InboxPage, publish_inbox_hint, query_inbox

if TYPE_CHECKING:
    from datetime import date, datetime
    from uuid import UUID

PAGE_LIMIT: Final = 100
DAY_STATUSES: Final = ("scheduled", "arrived", "in_progress", "completed", "no_show")
STARTABLE: Final = frozenset({"arrived", "in_progress"})
BRIEF_UNAVAILABLE: Final = "unavailable"


@dataclass(frozen=True, slots=True)
class TodayAppointment:
    """One of the actor's appointments today, in clinic-local time."""

    appointment_id: UUID
    start_at: datetime
    start_local: str
    end_local: str
    patient_label: str
    status: str
    encounter_open: bool
    can_start: bool


@dataclass(frozen=True, slots=True)
class TodayView:
    """The clinician's day; the brief stays unavailable until todo 44."""

    clinic: Clinic
    day: date
    appointments: tuple[TodayAppointment, ...]
    next_patient: TodayAppointment | None
    arrivals: tuple[TodayAppointment, ...]
    upcoming: int
    inbox: InboxPage | None
    limited: bool
    brief: str = BRIEF_UNAVAILABLE


def utc_now() -> datetime:
    """Return the current instant; tests pin the clinic day through this seam."""
    return timezone.now()


def _next_patient(
    rows: tuple[TodayAppointment, ...], now: datetime
) -> TodayAppointment | None:
    for status in ("in_progress", "arrived"):
        found = next((row for row in rows if row.status == status), None)
        if found is not None:
            return found
    return next(
        (row for row in rows if row.status == "scheduled" and row.start_at >= now),
        next((row for row in rows if row.status == "scheduled"), None),
    )


def view_today(*, clinic_id: UUID) -> TodayView:
    """Read the actor's own day; refusal is the permission boundary's denial."""
    actor = require_permission("appointment.read_own", clinic_id=clinic_id)
    clinic = Clinic.objects.get(pk=clinic_id)
    zone = str(clinic.timezone)
    now = utc_now()
    day = now.astimezone(ZoneInfo(zone)).date()
    lower, upper = civil_day_bounds(day, zone)
    found = tuple(
        Appointment.objects.filter(
            clinic_id=clinic_id,
            practitioner_id=actor,
            start_at__gte=lower,
            start_at__lt=upper,
            status__in=DAY_STATUSES,
        )
        .select_related("patient")
        .order_by("start_at", "pk")[: PAGE_LIMIT + 1]
    )
    opened = set(
        Encounter.objects.filter(
            appointment_id__in=[row.pk for row in found],
            state=Encounter.State.OPEN,
        ).values_list("appointment_id", flat=True)
    )
    rows = tuple(
        TodayAppointment(
            appointment_id=row.pk,
            start_at=row.start_at,
            start_local=row.start_at.astimezone(ZoneInfo(zone)).strftime("%H:%M"),
            end_local=row.end_at.astimezone(ZoneInfo(zone)).strftime("%H:%M"),
            patient_label=row.patient.full_name,
            status=row.status,
            encounter_open=row.pk in opened,
            can_start=row.status in STARTABLE,
        )
        for row in found[:PAGE_LIMIT]
    )
    try:
        inbox: InboxPage | None = query_inbox(clinic_id=clinic_id)
    except CurrentActorError:
        inbox = None
    return TodayView(
        clinic=clinic,
        day=day,
        appointments=rows,
        next_patient=_next_patient(rows, now),
        arrivals=tuple(row for row in rows if row.status == "arrived"),
        upcoming=sum(
            1 for row in rows if row.status == "scheduled" and row.start_at >= now
        ),
        inbox=inbox,
        limited=len(found) > PAGE_LIMIT,
    )


def appointment_changed(
    sender: type[Appointment], instance: Appointment, **kwargs: object
) -> None:
    """Hint connected Today boards after a booking, move or status change commits."""
    del sender, kwargs
    publish_inbox_hint(instance.clinic_id)


def register() -> None:
    """Connect the commit-bound Today refetch hint to appointment writes."""
    post_save.connect(
        appointment_changed,
        sender=Appointment,
        dispatch_uid="workflows.today.Appointment",
    )
