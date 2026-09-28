"""Recurring appointment series with 'this' / 'this and future' edits (todo 22).

A series is an immutable rule (weekly, biweekly or monthly by weekday, bounded
by count or until) materialized atomically into ordinary booked appointments.
Edits never delete occurrences: they cancel or move the addressed occurrences
through the lifecycle rules and append an immutable ``SeriesException``.
Occurrences keep clinic-local wall time; UTC is derived per date from the
clinic timezone, so zones without DST (Brazil since 2019) never shift.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from django.db import transaction

from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_creation import create_series_occurrence
from apps.scheduling.appointment_errors import AppointmentIdempotencyConflictError
from apps.scheduling.appointment_lifecycle import (
    ILLEGAL_TRANSITION,
    MOVE_TERMS,
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
    Term,
    _save,
)
from apps.scheduling.appointment_locking import (
    AppointmentWriteTarget,
    acquire_appointment_write_gates,
    lock_appointment_write_rows,
)
from apps.scheduling.appointment_persistence import update_appointment_range
from apps.scheduling.appointment_values import (
    AppointmentLocalRange,
    CreateAppointmentRequest,
    validate_appointment_syntax,
)
from apps.scheduling.lifecycle_models import MAX_SERIES_OCCURRENCES
from apps.scheduling.models import Appointment, AppointmentSeries, SeriesException
from apps.scheduling.patient_authority import (
    patient_booking_scope,
    record_appointment_event,
)
from apps.scheduling.resource_booking import authorized_service_clinic
from apps.scheduling.timezones import parse_local_minute

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apps.identity.models import Clinic

FREQUENCIES: Final = frozenset({"weekly", "biweekly", "monthly"})
SCOPES: Final = frozenset({"this", "future"})
EDIT_KINDS: Final = frozenset({"cancelled", "moved"})
MAX_MONTH_WEEK: Final = 4
MAX_UNTIL_DAYS: Final = 366
CANCELLABLE: Final = frozenset({"requested", "held", "scheduled"})
# Pinned series edit authority (SM S actor, RP agenda row); the own term binds
# the series owner (its practitioner).
EDIT_TERMS: Final[tuple[Term, ...]] = MOVE_TERMS
LOCAL_TIME_LENGTH: Final = 5


@dataclass(frozen=True, slots=True)
class SeriesBooking:
    """The first occurrence's local range plus the bounded recurrence rule."""

    local_range: AppointmentLocalRange
    frequency: str
    count: int | None = None
    until: date | None = None


@dataclass(frozen=True, slots=True)
class SeriesEdit:
    """Cancel or move occurrence ``occurrence_index`` (``this``) or onward."""

    kind: str
    occurrence_index: int
    scope: str
    start_local: str | None = None


def _add_months(value: date, months: int) -> tuple[int, int]:
    month_index = value.month - 1 + months
    return value.year + month_index // 12, month_index % 12 + 1


def series_dates(first: date, booking: SeriesBooking) -> tuple[date, ...]:
    """Return the bounded clinic-local occurrence dates, first included."""
    frequency, count, until = booking.frequency, booking.count, booking.until
    if (
        frequency not in FREQUENCIES
        or (count is None) == (until is None)
        or (
            count is not None
            and (type(count) is not int or not 1 <= count <= MAX_SERIES_OCCURRENCES)
        )
        or (
            until is not None
            and (
                type(until) is not date
                or until < first
                or until > first + timedelta(days=MAX_UNTIL_DAYS)
            )
        )
    ):
        raise AppointmentLifecycleInputError
    month_week = (first.day - 1) // 7 + 1
    if frequency == "monthly" and month_week > MAX_MONTH_WEEK:
        raise AppointmentLifecycleInputError
    dates: list[date] = []
    step = 0
    while len(dates) < (count or MAX_SERIES_OCCURRENCES + 1):
        if frequency == "monthly":
            year, month = _add_months(first, step)
            first_weekday = date(year, month, 1).weekday()
            day = 1 + (first.weekday() - first_weekday) % 7 + 7 * (month_week - 1)
            candidate = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        else:
            candidate = first + timedelta(
                days=step * (7 if frequency == "weekly" else 14)
            )
        if until is not None and candidate > until:
            break
        dates.append(candidate)
        step += 1
    if len(dates) > MAX_SERIES_OCCURRENCES:
        raise AppointmentLifecycleInputError
    return tuple(dates)


def _require_edit_authority(series: AppointmentSeries) -> None:
    if patient_booking_scope() is not None:
        raise AppointmentAccessDeniedError
    for term in EDIT_TERMS:
        try:
            actor = require_permission(term.permission, clinic_id=series.clinic_id)
        except CurrentActorError:
            continue
        if not term.own or actor == series.practitioner_id:
            return
    raise AppointmentAccessDeniedError


def _fingerprint(clinic: Clinic, values: dict[str, str]) -> bytes:
    canonical = json.dumps(
        values | {"clinic": str(clinic.pk)}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(
        b"clinic-appointment-series-v1\0" + canonical.encode()
    ).digest()


def create_series(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    practitioner_id: UUID,
    booking: SeriesBooking,
    idempotency_key: UUID,
) -> AppointmentSeries:
    """Book every occurrence of a bounded rule atomically, or none of them."""
    local_range = booking.local_range
    validate_appointment_syntax(local_range.start_local, local_range.end_local)
    if (
        type(idempotency_key) is not UUID
        or local_range.start_local[:10] != local_range.end_local[:10]
    ):
        raise AppointmentLifecycleInputError
    first = date.fromisoformat(local_range.start_local[:10])
    start_time = local_range.start_local[11:]
    end_time = local_range.end_local[11:]
    dates = series_dates(first, booking)
    with transaction.atomic():
        if patient_booking_scope() is not None:
            raise AppointmentAccessDeniedError
        clinic = authorized_service_clinic(clinic_id, practitioner_id)
        zone = clinic.timezone
        if not isinstance(zone, str):
            raise AppointmentLifecycleInputError
        fingerprint = _fingerprint(
            clinic,
            {
                "enrollment": str(enrollment_id),
                "practitioner": str(practitioner_id),
                "start": local_range.start_local,
                "end": local_range.end_local,
                "frequency": booking.frequency,
                "count": str(booking.count),
                "until": str(booking.until),
            },
        )
        existing = AppointmentSeries.objects.filter(
            organization_id=clinic.organization_id, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            if bytes(existing.create_fingerprint) != fingerprint:
                raise AppointmentIdempotencyConflictError
            return existing
        first_request = CreateAppointmentRequest(
            clinic_id, enrollment_id, practitioner_id, local_range, idempotency_key
        )
        series = AppointmentSeries.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            patient_id=_enrollment_patient(clinic, enrollment_id),
            practitioner_id=practitioner_id,
            frequency=booking.frequency,
            weekday=first.weekday(),
            month_week=(first.day - 1) // 7 + 1
            if booking.frequency == "monthly"
            else None,
            first_date=first,
            start_local=time.fromisoformat(start_time),
            end_local=time.fromisoformat(end_time),
            timezone=zone,
            count=booking.count,
            until=booking.until,
            idempotency_key=idempotency_key,
            create_fingerprint=fingerprint,
        )
        for index, day in enumerate(dates, start=1):
            create_series_occurrence(
                CreateAppointmentRequest(
                    first_request.clinic_id,
                    first_request.enrollment_id,
                    first_request.practitioner_id,
                    AppointmentLocalRange(f"{day}T{start_time}", f"{day}T{end_time}"),
                    uuid5(idempotency_key, f"occurrence:{index}"),
                ),
                series_id=series.pk,
                series_index=index,
            )
        record_appointment_event(
            "scheduling.series.created",
            clinic_id=clinic_id,
            affected_record_id=series.pk,
        )
        return series


def _enrollment_patient(clinic: Clinic, enrollment_id: UUID) -> UUID:
    try:
        return PatientClinicEnrollment.objects.get(
            organization_id=clinic.organization_id,
            clinic_id=clinic.pk,
            pk=enrollment_id,
        ).patient_id
    except PatientClinicEnrollment.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error


def _validate_edit(series_id: object, edit: object, command_id: object) -> SeriesEdit:
    if (
        type(series_id) is not UUID
        or type(command_id) is not UUID
        or not isinstance(edit, SeriesEdit)
        or edit.kind not in EDIT_KINDS
        or edit.scope not in SCOPES
        or type(edit.occurrence_index) is not int
        or not 1 <= edit.occurrence_index <= MAX_SERIES_OCCURRENCES
        or (edit.kind == "moved")
        != (
            isinstance(edit.start_local, str)
            and len(edit.start_local) == LOCAL_TIME_LENGTH
        )
    ):
        raise AppointmentLifecycleInputError
    if edit.kind == "moved":
        try:
            time.fromisoformat(str(edit.start_local))
        except ValueError as error:
            raise AppointmentLifecycleInputError from error
    return edit


def _moved_range(
    series: AppointmentSeries, occurrence: Appointment, start_local: str
) -> tuple[datetime, datetime]:
    zone = series.timezone
    duration = occurrence.end_at - occurrence.start_at
    day = occurrence.start_at.astimezone(_zone(zone)).date()
    start_at = parse_local_minute(f"{day}T{start_local}", zone)
    end_at = start_at + duration
    if end_at.astimezone(_zone(zone)).date() != day:
        raise AppointmentLifecycleInputError
    return start_at, end_at


def _zone(key: str) -> ZoneInfo:
    return ZoneInfo(key)


def _targets(series: AppointmentSeries, edit: SeriesEdit) -> tuple[Appointment, ...]:
    occurrences = Appointment.objects.select_for_update().filter(series=series)
    if edit.scope == "this":
        occurrences = occurrences.filter(series_index=edit.occurrence_index)
    else:
        occurrences = occurrences.filter(series_index__gte=edit.occurrence_index)
    return tuple(occurrences.order_by("series_index"))


def _actionable(
    edit: SeriesEdit, rows: Iterable[Appointment]
) -> tuple[Appointment, ...]:
    allowed = CANCELLABLE if edit.kind == "cancelled" else frozenset({"scheduled"})
    return tuple(row for row in rows if row.status in allowed)


def edit_series(
    *, clinic_id: UUID, series_id: UUID, edit: SeriesEdit, command_id: UUID
) -> tuple[Appointment, ...]:
    """Apply one 'this' or 'this and future' edit; occurrences are never deleted."""
    edit = _validate_edit(series_id, edit, command_id)
    if type(clinic_id) is not UUID:
        raise AppointmentLifecycleInputError
    with transaction.atomic():
        try:
            series = AppointmentSeries.objects.get(pk=series_id, clinic_id=clinic_id)
        except AppointmentSeries.DoesNotExist as error:
            raise AppointmentAccessDeniedError from error
        acquire_appointment_write_gates(
            target=AppointmentWriteTarget(
                organization_id=series.organization_id,
                clinic_id=series.clinic_id,
                patient_id=series.patient_id,
                practitioner_ids=(series.practitioner_id,),
            )
        )
        _require_edit_authority(series)
        prior = SeriesException.objects.filter(
            organization_id=series.organization_id, command_id=command_id
        ).first()
        rows = _targets(series, edit)
        if prior is not None:
            if (
                prior.series_id,
                prior.kind,
                prior.occurrence_index,
                prior.scope,
                None
                if prior.start_local is None
                else prior.start_local.strftime("%H:%M"),
            ) != (
                series.pk,
                edit.kind,
                edit.occurrence_index,
                edit.scope,
                edit.start_local,
            ):
                raise AppointmentIdempotencyConflictError
            return rows
        actionable = _actionable(edit, rows)
        if not actionable:
            raise AppointmentLifecycleError(ILLEGAL_TRANSITION)
        if edit.kind == "cancelled":
            for row in actionable:
                row.status = Appointment.Status.CANCELLED
                row.cancellation_reason = Appointment.CancellationReason.CLINIC_REQUEST
                row.last_command_id = uuid5(
                    command_id, f"occurrence:{row.series_index}"
                )
                _save(
                    row,
                    ("status", "cancellation_reason", "last_command_id", "updated_at"),
                )
        else:
            moves = tuple(
                (row, *_moved_range(series, row, str(edit.start_local)))
                for row in actionable
            )
            target = AppointmentWriteTarget(
                organization_id=series.organization_id,
                clinic_id=series.clinic_id,
                patient_id=series.patient_id,
                practitioner_ids=(series.practitioner_id,),
            )
            for row, start_at, end_at in moves:
                lock_appointment_write_rows(
                    target=target,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    additional_ranges=((start_at, end_at),),
                    appointment_ids=(row.pk,),
                )
                update_appointment_range(row, start_at=start_at, end_at=end_at)
        SeriesException.objects.create(
            organization_id=series.organization_id,
            clinic_id=series.clinic_id,
            series=series,
            occurrence_index=edit.occurrence_index,
            scope=edit.scope,
            kind=edit.kind,
            start_local=None
            if edit.start_local is None
            else time.fromisoformat(edit.start_local),
            command_id=command_id,
            affected_count=len(actionable),
        )
        record_appointment_event(
            "scheduling.series.edited",
            clinic_id=clinic_id,
            affected_record_id=series.pk,
        )
        return tuple(
            Appointment.objects.filter(pk__in=[row.pk for row in rows]).order_by(
                "series_index"
            )
        )
