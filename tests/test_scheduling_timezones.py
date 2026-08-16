from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from apps.scheduling import timezones
from apps.scheduling.timezones import (
    LocalTimeValueError,
    civil_day_bounds,
    parse_local_minute,
)
from django.db import connection
from psycopg import sql


def test_local_minute_round_trips_through_an_explicit_iana_zone() -> None:
    instant = parse_local_minute("2030-01-02T09:30", "America/Manaus")

    assert instant == datetime(2030, 1, 2, 13, 30, tzinfo=UTC)
    assert timezones.format_local_minute(instant, "America/Manaus") == (
        "2030-01-02T09:30"
    )


def test_local_minute_rejects_a_nonexistent_gap_instant() -> None:
    with pytest.raises(LocalTimeValueError, match="ambiguous or nonexistent"):
        parse_local_minute("2024-03-10T02:30", "America/New_York")


def test_local_minute_rejects_an_ambiguous_fold_instant() -> None:
    with pytest.raises(LocalTimeValueError, match="ambiguous or nonexistent"):
        parse_local_minute("2024-11-03T01:30", "America/New_York")


def test_civil_day_starts_at_first_valid_post_gap_instant() -> None:
    assert civil_day_bounds(date(2018, 11, 4), "America/Sao_Paulo") == (
        datetime(2018, 11, 4, 3, 0, tzinfo=UTC),
        datetime(2018, 11, 5, 2, 0, tzinfo=UTC),
    )


def test_civil_day_chooses_the_earlier_midnight_fold() -> None:
    assert civil_day_bounds(date(2020, 11, 1), "America/Havana") == (
        datetime(2020, 11, 1, 4, 0, tzinfo=UTC),
        datetime(2020, 11, 2, 5, 0, tzinfo=UTC),
    )


def test_wholly_skipped_civil_date_has_empty_bounds() -> None:
    skipped_boundary = datetime(2011, 12, 30, 10, 0, tzinfo=UTC)

    assert civil_day_bounds(date(2011, 12, 30), "Pacific/Apia") == (
        skipped_boundary,
        skipped_boundary,
    )


def test_civil_week_uses_monday_boundaries() -> None:
    assert timezones.civil_week_bounds(date(2018, 11, 4), "America/Sao_Paulo") == (
        datetime(2018, 10, 29, 3, 0, tzinfo=UTC),
        datetime(2018, 11, 5, 2, 0, tzinfo=UTC),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "table_name",
    [
        "intake_patientclinicenrollment",
        "scheduling_appointment",
        "scheduling_availabilityblock",
    ],
)
def test_timezone_change_refuses_after_first_dependent_row(table_name: str) -> None:
    clinic_id = uuid4()
    table = sql.Identifier(table_name)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE TABLE clinic_app.{} (clinic_id uuid NOT NULL)").format(
                table
            )
        )
        cursor.execute(
            sql.SQL("INSERT INTO clinic_app.{} (clinic_id) VALUES (%s)").format(table),
            [clinic_id],
        )
    try:
        with pytest.raises(timezones.ClinicTimezoneLockedError):
            timezones.ensure_clinic_timezone_change_allowed(clinic_id)
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP TABLE clinic_app.{}").format(table))
