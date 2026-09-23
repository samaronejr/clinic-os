"""Strict IANA-zone conversion at scheduling form boundaries."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import connection, models
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from uuid import UUID

    class _IanaTimezoneFieldBase(models.CharField[str, str | None]): ...

else:

    class _IanaTimezoneFieldBase(models.CharField): ...


LOCAL_MINUTE_PATTERN: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
)
LOCAL_MINUTE_FORMAT: Final = "%Y-%m-%dT%H:%M"
MINUTES_PER_DAY: Final = 24 * 60
INVALID_TIMEZONE_MESSAGE: Final = _("Enter a valid IANA timezone.")
CLINIC_DEPENDENCY_QUERIES: Final = (
    (
        "clinic_app.intake_patientclinicenrollment",
        "SELECT EXISTS (SELECT 1 FROM "
        "clinic_app.intake_patientclinicenrollment WHERE clinic_id = %s)",
    ),
    (
        "clinic_app.scheduling_availabilityblock",
        "SELECT EXISTS (SELECT 1 FROM "
        "clinic_app.scheduling_availabilityblock WHERE clinic_id = %s)",
    ),
    (
        "clinic_app.scheduling_appointment",
        "SELECT EXISTS (SELECT 1 FROM "
        "clinic_app.scheduling_appointment WHERE clinic_id = %s)",
    ),
)


class TimezoneValueError(ValueError):
    """Reject a blank or unavailable IANA timezone key."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("timezone must be a nonblank IANA key")


class LocalTimeValueError(ValueError):
    """Reject malformed, ambiguous, nonexistent, or non-UTC minute values."""


class ClinicTimezoneLockedError(RuntimeError):
    """Refuse timezone changes after clinic-dependent durable data exists."""

    def __init__(self) -> None:
        """Expose one stable non-identifying immutability message."""
        super().__init__("clinic timezone is immutable")


class ClinicTimezoneStateError(RuntimeError):
    """Fail closed when a dependency query returns an invalid shape."""

    def __init__(self) -> None:
        """Expose one stable non-identifying state message."""
        super().__init__("clinic dependency state unavailable")


class _LocalMinuteFormatError(LocalTimeValueError):
    def __init__(self) -> None:
        super().__init__("local time must use YYYY-MM-DDTHH:MM")


class _LocalMinuteAmbiguityError(LocalTimeValueError):
    def __init__(self) -> None:
        super().__init__("local time is ambiguous or nonexistent")


class _UtcMinuteError(LocalTimeValueError):
    def __init__(self) -> None:
        super().__init__("instant must be an aware UTC minute")


class _CivilBoundaryError(LocalTimeValueError):
    def __init__(self) -> None:
        super().__init__("civil date has no valid local instant")


def validate_iana_timezone(value: str) -> None:
    """Validate one required IANA timezone key for Django model boundaries."""
    try:
        _zone(value)
    except TimezoneValueError as error:
        raise ValidationError(
            INVALID_TIMEZONE_MESSAGE,
            code="invalid_timezone",
        ) from error


class IanaTimezoneField(_IanaTimezoneFieldBase):
    """Validate the IANA key on form cleaning and every model save path."""

    def pre_save(self, model_instance: models.Model, add: bool) -> str:
        """Reject invalid values before Django constructs the write query."""
        del add
        value = getattr(model_instance, self.attname, None)
        if not isinstance(value, str):
            raise ValidationError(
                INVALID_TIMEZONE_MESSAGE,
                code="invalid_timezone",
            )
        validate_iana_timezone(value)
        return value


def _zone(timezone_key: str) -> ZoneInfo:
    if not timezone_key or timezone_key != timezone_key.strip():
        raise TimezoneValueError
    try:
        return ZoneInfo(timezone_key)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise TimezoneValueError from error


def _utc_candidates(local_value: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    candidates: set[datetime] = set()
    for fold in (0, 1):
        candidate = local_value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=UTC) == local_value:
            candidates.add(candidate)
    return tuple(sorted(candidates))


def parse_local_minute(value: str, timezone_key: str) -> datetime:
    """Convert one unambiguous local form minute to an aware UTC instant."""
    if LOCAL_MINUTE_PATTERN.fullmatch(value) is None:
        raise _LocalMinuteFormatError
    try:
        local_value = datetime.strptime(f"{value}+0000", f"{LOCAL_MINUTE_FORMAT}%z")
    except ValueError as error:
        raise _LocalMinuteFormatError from error
    candidates = _utc_candidates(local_value, _zone(timezone_key))
    if len(candidates) != 1:
        raise _LocalMinuteAmbiguityError
    return candidates[0]


def format_local_minute(value_utc: datetime, timezone_key: str) -> str:
    """Format one aware, minute-precise UTC instant in an explicit IANA zone."""
    if (
        value_utc.utcoffset() != timedelta(0)
        or value_utc.second != 0
        or value_utc.microsecond != 0
    ):
        raise _UtcMinuteError
    return value_utc.astimezone(_zone(timezone_key)).strftime(LOCAL_MINUTE_FORMAT)


def civil_boundary(day: date, timezone_key: str) -> datetime:
    """Return the first valid UTC instant represented by one civil date."""
    zone = _zone(timezone_key)
    candidate_day = day
    while True:
        midnight = datetime.combine(candidate_day, time.min, tzinfo=UTC)
        for minute in range(MINUTES_PER_DAY):
            candidates = _utc_candidates(midnight + timedelta(minutes=minute), zone)
            if candidates:
                return candidates[0]
        if candidate_day == date.max:
            raise _CivilBoundaryError
        candidate_day += timedelta(days=1)


def civil_day_bounds(day: date, timezone_key: str) -> tuple[datetime, datetime]:
    """Return the UTC half-open interval for one civil date."""
    return (
        civil_boundary(day, timezone_key),
        civil_boundary(day + timedelta(days=1), timezone_key),
    )


def civil_week_bounds(day: date, timezone_key: str) -> tuple[datetime, datetime]:
    """Return Monday-to-Monday UTC bounds for the containing ISO week."""
    monday = day - timedelta(days=day.weekday())
    return (
        civil_boundary(monday, timezone_key),
        civil_boundary(monday + timedelta(days=7), timezone_key),
    )


def ensure_clinic_timezone_change_allowed(clinic_id: UUID) -> None:
    """Require every installed enrollment or scheduling relation to be empty."""
    with connection.cursor() as cursor:
        for relation, query in CLINIC_DEPENDENCY_QUERIES:
            cursor.execute("SELECT pg_catalog.to_regclass(%s)", [relation])
            relation_row = cursor.fetchone()
            if (
                relation_row is None
                or not isinstance(relation_row, tuple)
                or len(relation_row) != 1
            ):
                raise ClinicTimezoneStateError
            if relation_row[0] is None:
                continue
            cursor.execute(query, [clinic_id])
            exists_row = cursor.fetchone()
            if exists_row == (True,):
                raise ClinicTimezoneLockedError
            if exists_row != (False,):
                raise ClinicTimezoneStateError
