"""Multi-resource agenda board: day, week, month and list over one clinic.

The board composes the day grid's authority: ``authorized_agenda_scope``
decides who may read (booking staff see every professional, a physician only
themself), ``_move_rules`` mirrors the move terms and ``authorized_service_clinic``
the booking terms, so every hook the board renders is one the services would
accept. Lanes are professionals, rooms and equipment. Filters narrow the
appointments shown; occupancy is always computed from every capacity-consuming
booking, so a hidden appointment still makes its time busy.

A professional's bookings in another unit (clinic) of the same organization
appear as busy intervals without any patient, clinic or service detail.
Cross-organization busy time is a disabled capability until a separate owner
decision (``CROSS_ORGANIZATION_BUSY``); row security already confines every
read here to the current organization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    authorized_agenda_scope,
)
from apps.scheduling.agenda_grid import (
    FIRST_SLOT,
    LAST_SLOT_END,
    SLOT_MINUTES,
    _columns,
    _holds,
    _move_rules,
    _MoveRules,
)
from apps.scheduling.agenda_queries import AGENDA_STATUSES, AgendaInputError, _inputs
from apps.scheduling.appointment_locking import due_hold
from apps.scheduling.models import (
    Appointment,
    AvailabilityBlock,
    Resource,
    ServiceType,
)
from apps.scheduling.timezones import civil_day_bounds, format_local_minute

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet

    from apps.scheduling.access import AvailabilityViewScope

BOARD_VIEWS: Final = ("day", "week", "month", "list")
LANE_KINDS: Final = ("practitioner", "room", "equipment")
MAX_BOARD_APPOINTMENTS: Final = 400
MAX_FREE_SLOTS: Final = 48
# A hold this close to its deadline renders the hold-expiring state.
HOLD_WARNING: Final = timedelta(minutes=2)
# Opt-in cross-organization busy time stays off until a separate owner
# decision; nothing reads another organization's bookings.
CROSS_ORGANIZATION_BUSY: Final = False
LOCAL_DATE: Final = slice(0, 10)
LOCAL_TIME: Final = slice(11, 16)
_RESOURCE_KINDS: Final = (Resource.Kind.ROOM, Resource.Kind.EQUIPMENT)


@dataclass(frozen=True, slots=True)
class AgendaFilters:
    """Optional board filters; the unit filter is the clinic itself."""

    professional_id: UUID | None = None
    resource_id: UUID | None = None
    service_type_id: UUID | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class BoardLane:
    """One professional, room or equipment lane, in display order."""

    kind: str
    lane_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class BoardAppointment:
    """One visible appointment with its lanes, revision and allowed actions."""

    appointment_id: UUID
    patient_display_name: str
    practitioner_id: UUID
    resource_ids: tuple[UUID, ...]
    service_type_id: UUID | None
    start_local: str
    end_local: str
    status: str
    revision: int
    movable: bool
    actions: tuple[str, ...]
    hold_until: str = ""
    hold_expiring: bool = False

    @property
    def day(self) -> str:
        """Clinic-local YYYY-MM-DD start date."""
        return self.start_local[LOCAL_DATE]

    @property
    def start_time(self) -> str:
        """Clinic-local HH:MM start."""
        return self.start_local[LOCAL_TIME]

    @property
    def end_time(self) -> str:
        """Clinic-local HH:MM end."""
        return self.end_local[LOCAL_TIME]

    @property
    def duration_minutes(self) -> int:
        """Whole minutes between start and end (one clinic-local date)."""
        return minute_of(self.end_time) - minute_of(self.start_time)

    @property
    def occupies(self) -> bool:
        """Whether the lifecycle state consumes capacity."""
        return self.status in Appointment.OCCUPYING_STATUSES

    @property
    def appearance(self) -> str:
        """Reuse the resource grid's semantic lifecycle treatments."""
        appearances: dict[str, str] = {
            Appointment.Status.REQUESTED: "held",
            Appointment.Status.HELD: "held",
            Appointment.Status.COMPLETED: "done",
            Appointment.Status.CANCELLED: "inactive",
            Appointment.Status.NO_SHOW: "inactive",
        }
        return appearances.get(self.status, "booked")

    def in_lane(self, lane: BoardLane) -> bool:
        """Whether the appointment occupies this professional or resource lane."""
        if lane.kind == "practitioner":
            return self.practitioner_id == lane.lane_id
        return lane.lane_id in self.resource_ids


@dataclass(frozen=True, slots=True)
class Occupancy:
    """A capacity-consuming interval on one lane; ``other_unit`` hides detail."""

    lane_id: UUID
    start_local: str
    end_local: str
    other_unit: bool = False

    @property
    def day(self) -> str:
        """Clinic-local YYYY-MM-DD start date."""
        return self.start_local[LOCAL_DATE]


@dataclass(frozen=True, slots=True)
class DayCount:
    """How many visible appointments start on one clinic-local date."""

    day: date
    total: int


@dataclass(frozen=True, slots=True)
class ServiceOption:
    """One active service type offered as a filter and booking choice."""

    service_type_id: UUID
    label: str
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class AgendaBoard:
    """One authorized clinic-local board read."""

    view: str
    anchor: date
    first_day: date
    last_day: date
    timezone_key: str
    lanes: tuple[BoardLane, ...]
    days: tuple[date, ...]
    slots: tuple[str, ...]
    appointments: tuple[BoardAppointment, ...]
    occupancy: tuple[Occupancy, ...]
    counts: tuple[DayCount, ...]
    professionals: tuple[BoardLane, ...]
    resources: tuple[BoardLane, ...]
    services: tuple[ServiceOption, ...]
    filters: AgendaFilters
    can_move: bool
    can_book: bool
    own_practitioner: UUID | None
    truncated: bool
    subject: BoardLane | None = None
    statuses: tuple[str, ...] = field(default=AGENDA_STATUSES)
    can_transition: bool = False


@dataclass(frozen=True, slots=True)
class BoardCatalog:
    """The professionals, resources and services the actor may filter by."""

    professionals: tuple[BoardLane, ...]
    resources: tuple[BoardLane, ...]
    services: tuple[ServiceOption, ...]

    def _allowed(self) -> tuple[set[UUID], set[UUID], set[UUID]]:
        return (
            {lane.lane_id for lane in self.professionals},
            {lane.lane_id for lane in self.resources},
            {option.service_type_id for option in self.services},
        )

    def admits(self, filters: AgendaFilters) -> bool:
        """Report whether every filter names an entry of this catalog."""
        professionals, resources, services = self._allowed()
        return (
            filters.professional_id in {None, *professionals}
            and filters.resource_id in {None, *resources}
            and filters.service_type_id in {None, *services}
        )

    def known(self, filters: AgendaFilters) -> AgendaFilters:
        """Drop filters whose catalog entry is gone (a retired resource)."""
        professionals, resources, services = self._allowed()
        return AgendaFilters(
            professional_id=filters.professional_id
            if filters.professional_id in professionals
            else None,
            resource_id=filters.resource_id
            if filters.resource_id in resources
            else None,
            service_type_id=filters.service_type_id
            if filters.service_type_id in services
            else None,
            status=filters.status,
        )


@dataclass(frozen=True, slots=True)
class FreeSlots:
    """Clinic-local start times one booking could move to on one date."""

    day: date
    starts: tuple[str, ...]


def minute_of(hhmm: str) -> int:
    """Minutes since local midnight of an HH:MM value."""
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _range(view: str, anchor: date) -> tuple[date, date]:
    if view == "day":
        return anchor, anchor
    if view == "month":
        first = anchor.replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        return first, following - timedelta(days=1)
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=6)


def _bounds(first: date, last: date, timezone_key: str) -> tuple[datetime, datetime]:
    """Real zone transitions: each end is the next civil day's first instant."""
    return civil_day_bounds(first, timezone_key)[0], civil_day_bounds(
        last, timezone_key
    )[1]


def _days(first: date, last: date) -> tuple[date, ...]:
    return tuple(first + timedelta(days=n) for n in range((last - first).days + 1))


def _slots(starts: list[str], ends: list[str]) -> tuple[str, ...]:
    first = min((minute_of(value) for value in starts), default=FIRST_SLOT)
    last = max((minute_of(value) for value in ends), default=0)
    start = min(FIRST_SLOT, first - first % SLOT_MINUTES)
    end = max(LAST_SLOT_END, last)
    return tuple(_hhmm(minute) for minute in range(start, end, SLOT_MINUTES))


def _catalog(
    scope: AvailabilityViewScope, clinic_id: UUID, booked: set[UUID]
) -> tuple[tuple[BoardLane, ...], tuple[BoardLane, ...], tuple[ServiceOption, ...]]:
    professionals = tuple(
        BoardLane("practitioner", pk, label)
        for pk, label in _columns(scope, booked).items()
    )
    resources = tuple(
        BoardLane(str(row.kind), row.pk, row.name)
        for row in Resource.objects.filter(
            clinic_id=clinic_id, kind__in=_RESOURCE_KINDS, active=True
        ).order_by("kind", "name", "pk")
    )
    services = tuple(
        ServiceOption(row.pk, row.name, row.duration_min)
        for row in ServiceType.objects.filter(
            clinic_id=clinic_id, active=True
        ).order_by("name", "pk")
    )
    return professionals, resources, services


@dataclass(frozen=True)
class _Grant:
    """One transition term the viewer holds; own-schedule grants name the actor."""

    actor: UUID | None
    own: bool


def _book_grants(clinic_id: UUID) -> tuple[_Grant, ...]:
    """Ask the booking terms in ``authorized_service_clinic`` order: book, then own."""
    if _holds("appointment.book", clinic_id) is not None:
        return (_Grant(None, own=False),)
    actor = _holds("appointment.book_own", clinic_id)
    return () if actor is None else (_Grant(actor, own=True),)


def _move_grants(rules: _MoveRules) -> tuple[_Grant, ...]:
    """Reuse the move terms ``_move_rules`` already asked, in the same order."""
    every = (_Grant(None, own=False),) if rules.service else ()
    own = () if rules.own is None else (_Grant(rules.own, own=True),)
    return every + own


def _admits(grants: tuple[_Grant, ...], row: Appointment) -> bool:
    """Match own-schedule grants only to a booked practitioner, never to null."""
    return any(
        not grant.own
        or (row.practitioner_id is not None and grant.actor == row.practitioner_id)
        for grant in grants
    )


def _actions(
    row: Appointment,
    *,
    book: tuple[_Grant, ...],
    move: tuple[_Grant, ...],
    now: datetime,
) -> tuple[str, ...]:
    """Offer exactly the transitions ``authorize_transition`` would accept."""
    moving = _admits(move, row)
    actions: list[str] = []
    pending = {Appointment.Status.REQUESTED, Appointment.Status.HELD}
    if row.status in pending and _admits(book, row):
        actions.append("confirm")
    if row.status == Appointment.Status.SCHEDULED and moving:
        actions.append("arrive")
        if row.start_at <= now:
            actions.append("no_show")
    if moving and row.status in {
        Appointment.Status.REQUESTED,
        Appointment.Status.HELD,
        Appointment.Status.SCHEDULED,
        Appointment.Status.ARRIVED,
    }:
        actions.append("cancel")
    return tuple(actions)


def _filtered(
    rows: QuerySet[Appointment], filters: AgendaFilters
) -> QuerySet[Appointment]:
    if filters.professional_id is not None:
        rows = rows.filter(practitioner_id=filters.professional_id)
    if filters.resource_id is not None:
        rows = rows.filter(resource_ids__contains=[filters.resource_id])
    if filters.service_type_id is not None:
        rows = rows.filter(service_type_id=filters.service_type_id)
    if filters.status is not None:
        rows = rows.filter(status=filters.status)
    return rows


def _visible_lanes(
    view: str,
    filters: AgendaFilters,
    professionals: tuple[BoardLane, ...],
    resources: tuple[BoardLane, ...],
) -> tuple[tuple[BoardLane, ...], BoardLane | None]:
    """Return the board's lanes and, for the week view, its single subject."""
    chosen_professionals = tuple(
        lane
        for lane in professionals
        if filters.professional_id in {None, lane.lane_id}
    )
    chosen_resources = tuple(
        lane for lane in resources if filters.resource_id in {None, lane.lane_id}
    )
    if view != "week":
        return (*chosen_professionals, *chosen_resources), None
    if filters.professional_id is not None:
        subject = next(iter(chosen_professionals), None)
    elif filters.resource_id is not None:
        subject = next(iter(chosen_resources), None)
    else:
        subject = next(iter((*professionals, *resources)), None)
    return (() if subject is None else (subject,)), subject


def _occupancy(
    clinic_id: UUID,
    organization_id: UUID,
    lanes: tuple[BoardLane, ...],
    interval: tuple[datetime, datetime],
    timezone_key: str,
) -> tuple[Occupancy, ...]:
    """Read times only: no patient, service or other-unit identity leaves."""
    start_at, end_at = interval
    practitioners = {lane.lane_id for lane in lanes if lane.kind == "practitioner"}
    resources = {lane.lane_id for lane in lanes if lane.kind != "practitioner"}
    occupying = (
        Appointment.objects.filter(
            organization_id=organization_id,
            status__in=Appointment.OCCUPYING_STATUSES,
            start_at__lt=end_at,
            end_at__gt=start_at,
        )
        .exclude(due_hold())
        .order_by("start_at", "pk")
    )
    found: list[Occupancy] = []
    for practitioner_id, resource_ids, start, end, unit in occupying.filter(
        Q(clinic_id=clinic_id) | Q(practitioner_id__in=practitioners)
    ).values_list("practitioner_id", "resource_ids", "start_at", "end_at", "clinic_id"):
        other = unit != clinic_id
        start_local = format_local_minute(start, timezone_key)
        end_local = format_local_minute(end, timezone_key)
        if practitioner_id in practitioners:
            found.append(Occupancy(practitioner_id, start_local, end_local, other))
        if not other:
            found.extend(
                Occupancy(pk, start_local, end_local)
                for pk in resource_ids
                if pk in resources
            )
    return tuple(found)


def _counts(
    rows: QuerySet[Appointment], days: tuple[date, ...], timezone_key: str
) -> tuple[DayCount, ...]:
    totals = dict.fromkeys(days, 0)
    for (start,) in rows.values_list("start_at"):
        local = date.fromisoformat(format_local_minute(start, timezone_key)[LOCAL_DATE])
        if local in totals:
            totals[local] += 1
    return tuple(DayCount(day, total) for day, total in totals.items())


def _presented(  # noqa: PLR0913 - one row needs its full authority context
    row: Appointment,
    rules: _MoveRules,
    *,
    book: tuple[_Grant, ...],
    move: tuple[_Grant, ...],
    lane_resources: set[UUID],
    timezone_key: str,
    now: datetime,
) -> BoardAppointment:
    held = row.status == Appointment.Status.HELD and row.hold_expires_at is not None
    deadline = row.hold_expires_at if held else None
    return BoardAppointment(
        appointment_id=row.pk,
        patient_display_name=row.patient.full_name,
        practitioner_id=row.practitioner_id,
        resource_ids=tuple(pk for pk in row.resource_ids if pk in lane_resources),
        service_type_id=row.service_type_id,
        start_local=format_local_minute(row.start_at, timezone_key),
        end_local=format_local_minute(row.end_at, timezone_key),
        status=row.status,
        revision=row.revision,
        movable=rules.allows(row),
        actions=_actions(row, book=book, move=move, now=now),
        hold_until=""
        if deadline is None
        else format_local_minute(
            deadline.replace(second=0, microsecond=0), timezone_key
        )[LOCAL_TIME],
        hold_expiring=deadline is not None and deadline - now <= HOLD_WARNING,
    )


def view_agenda_board(
    *,
    clinic_id: UUID,
    view: str,
    date: str,
    filters: AgendaFilters | None = None,
) -> AgendaBoard:
    """Return one authorized clinic-local day, week, month or list board."""
    selected = AgendaFilters() if filters is None else filters
    if type(clinic_id) is not UUID or not isinstance(selected, AgendaFilters):
        raise AvailabilityAccessDeniedError
    identifiers = (
        selected.professional_id,
        selected.resource_id,
        selected.service_type_id,
    )
    if any(value is not None and type(value) is not UUID for value in identifiers) or (
        selected.status is not None and selected.status not in AGENDA_STATUSES
    ):
        raise AgendaInputError
    with transaction.atomic():
        scope = authorized_agenda_scope(clinic_id)
        timezone_key = scope.clinic.timezone
        if not isinstance(timezone_key, str):
            raise AvailabilityAccessDeniedError
        if view not in BOARD_VIEWS:
            raise AgendaInputError
        _, anchor, _ = _inputs("day", date, 1)
        first_day, last_day = _range(view, anchor)
        interval = _bounds(first_day, last_day, timezone_key)
        rows = Appointment.objects.filter(
            organization_id=scope.clinic.organization_id,
            clinic_id=clinic_id,
            start_at__gte=interval[0],
            start_at__lt=interval[1],
            status__in=AGENDA_STATUSES,
        ).exclude(due_hold())
        if scope.practitioner_id is not None:
            rows = rows.filter(practitioner_id=scope.practitioner_id)
        booked = set(rows.values_list("practitioner_id", flat=True).distinct())
        professionals, resources, services = _catalog(scope, clinic_id, booked)
        if not BoardCatalog(professionals, resources, services).admits(selected):
            raise AgendaInputError
        rules = _move_rules(clinic_id)
        book = _book_grants(clinic_id)
        move = _move_grants(rules)
        can_book = bool(book)
        lanes, subject = _visible_lanes(view, selected, professionals, resources)
        shown = _filtered(rows, selected)
        counts: tuple[DayCount, ...] = ()
        appointments: tuple[BoardAppointment, ...] = ()
        occupancy: tuple[Occupancy, ...] = ()
        truncated = False
        if view == "month":
            counts = _counts(shown, _days(first_day, last_day), timezone_key)
        else:
            ordered = tuple(
                shown.select_related("patient").order_by(
                    "start_at", "practitioner_id", "pk"
                )[: MAX_BOARD_APPOINTMENTS + 1]
            )
            truncated = len(ordered) > MAX_BOARD_APPOINTMENTS
            lane_resources = {lane.lane_id for lane in resources}
            now = timezone.now()
            appointments = tuple(
                _presented(
                    row,
                    rules,
                    book=book,
                    move=move,
                    lane_resources=lane_resources,
                    timezone_key=timezone_key,
                    now=now,
                )
                for row in ordered[:MAX_BOARD_APPOINTMENTS]
            )
            if view != "list":
                occupancy = _occupancy(
                    clinic_id,
                    scope.clinic.organization_id,
                    lanes,
                    interval,
                    timezone_key,
                )
        board = AgendaBoard(
            view=view,
            anchor=anchor,
            first_day=first_day,
            last_day=last_day,
            timezone_key=timezone_key,
            lanes=lanes,
            days=_days(first_day, last_day),
            slots=_slots(
                [item.start_time for item in appointments],
                [item.end_time for item in appointments],
            ),
            appointments=appointments,
            occupancy=occupancy,
            counts=counts,
            professionals=professionals,
            resources=resources,
            services=services,
            filters=selected,
            can_move=rules.any,
            can_book=can_book,
            own_practitioner=scope.practitioner_id,
            truncated=truncated,
            subject=subject,
            can_transition=bool(book or move),
        )
        record_phase1_event(
            "scheduling.agenda.viewed",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )
        return board


def board_catalog(*, clinic_id: UUID) -> BoardCatalog:
    """Return the authorized filter catalog without reading any appointment."""
    if type(clinic_id) is not UUID:
        raise AvailabilityAccessDeniedError
    with transaction.atomic():
        scope = authorized_agenda_scope(clinic_id)
        return BoardCatalog(*_catalog(scope, clinic_id, set()))


def free_move_slots(*, clinic_id: UUID, appointment_id: UUID, date: str) -> FreeSlots:
    """Find where one movable booking could start on a clinic-local date.

    Candidates are half-hour starts inside the professional's open availability
    that overlap none of their capacity-consuming bookings (in any unit of the
    organization). It is advice for the move dialog: the move itself rechecks
    rooms, buffers and every exclusion under its locks.
    """
    if type(clinic_id) is not UUID or type(appointment_id) is not UUID:
        raise AvailabilityAccessDeniedError
    with transaction.atomic():
        scope = authorized_agenda_scope(clinic_id)
        row = Appointment.objects.filter(
            pk=appointment_id,
            clinic_id=clinic_id,
            status=Appointment.Status.SCHEDULED,
        ).first()
        # Only a booking the board would render a move hook for: unknown,
        # foreign, another physician's and unmovable ones share one refusal.
        if (
            row is None
            or scope.practitioner_id not in {None, row.practitioner_id}
            or not _move_rules(clinic_id).allows(row)
        ):
            raise AvailabilityAccessDeniedError
        timezone_key = row.clinic.timezone
        if not isinstance(timezone_key, str):
            raise AvailabilityAccessDeniedError
        _, day, _ = _inputs("day", date, 1)
        start_at, end_at = civil_day_bounds(day, timezone_key)
        duration = row.end_at - row.start_at
        windows = tuple(
            AvailabilityBlock.objects.filter(
                clinic_id=clinic_id,
                practitioner_id=row.practitioner_id,
                retired_at__isnull=True,
                start_at__lt=end_at,
                end_at__gt=start_at,
            )
            .filter(Q(template__isnull=True) | Q(template__active=True))
            .order_by("start_at")
            .values_list("start_at", "end_at")
        )
        busy = tuple(
            Appointment.objects.filter(
                organization_id=row.organization_id,
                practitioner_id=row.practitioner_id,
                status__in=Appointment.OCCUPYING_STATUSES,
                start_at__lt=end_at,
                end_at__gt=start_at,
            )
            .exclude(pk=row.pk)
            .exclude(due_hold())
            .values_list("start_at", "end_at")
        )
        now = timezone.now()
        starts: list[str] = []
        candidate = start_at
        step = timedelta(minutes=SLOT_MINUTES)
        while candidate < end_at and len(starts) < MAX_FREE_SLOTS:
            finish = candidate + duration
            if (
                candidate > now
                and any(begin <= candidate and finish <= end for begin, end in windows)
                and not any(begin < finish and candidate < end for begin, end in busy)
            ):
                starts.append(format_local_minute(candidate, timezone_key)[LOCAL_TIME])
            candidate += step
        return FreeSlots(day=day, starts=tuple(starts))


__all__ = (
    "BOARD_VIEWS",
    "CROSS_ORGANIZATION_BUSY",
    "AgendaBoard",
    "AgendaFilters",
    "BoardAppointment",
    "BoardCatalog",
    "BoardLane",
    "DayCount",
    "FreeSlots",
    "Occupancy",
    "ServiceOption",
    "board_catalog",
    "free_move_slots",
    "view_agenda_board",
)
