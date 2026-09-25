"""Scoped scheduling clocks; audit/auth remain on their separate DB clock."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from apps.scheduling import (
    agenda_presenter,
    appointment_cancellation,
    appointment_rescheduling,
    appointment_values,
    availability_creation,
    availability_retirement,
    booking_queries,
    patient_views,
    waitlist,
)
from django.db import models
from django.utils import timezone

from scheduling.clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from collections.abc import Iterator


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--scheduling-ambient-year",
        type=int,
        choices=(2035, 2040),
        default=None,
        help="Advance ambient scheduling Python and SQL clocks for the full suite.",
    )


@pytest.fixture(autouse=True)
def controlled_scheduling_clock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[datetime | None]:
    """Keep fixed legacy/resource windows independent of ambient scheduling time.

    Ordinary non-resource tests are unchanged unless the explicit ambient-clock
    verification option is used. Resource tests always use their fixed reference.
    Scheduling ORM timestamps follow the calendar reference. Under ambient
    perturbation other domains' timestamps stay on the independent DB-time axis.
    """
    year = request.config.getoption("--scheduling-ambient-year")
    resource_test = "resource_clock" in request.fixturenames
    if not resource_test and year is None:
        yield None
        return
    request.getfixturevalue("db")
    database_url = request.getfixturevalue("superuser_database_url")
    reference = (
        datetime(2035, 6, 1, tzinfo=UTC)
        if resource_test
        else datetime(2026, 9, 25, tzinfo=UTC)
    )
    ambient = None if year is None else datetime(year, 1, 1, tzinfo=UTC)
    clock = SimpleNamespace(**vars(timezone))
    clock.now = lambda: reference
    for module in (
        agenda_presenter,
        availability_creation,
        availability_retirement,
        appointment_values,
        appointment_rescheduling,
        appointment_cancellation,
        booking_queries,
        patient_views,
        waitlist,
    ):
        monkeypatch.setattr(module, "timezone", clock)
    # These two modules' time reads create permission grants, not calendar rows.
    authority_modules = {
        "scheduling.test_resource_authority",
        "scheduling.test_service_booking_http_authority",
    }
    if request.module.__name__ not in authority_modules and hasattr(
        request.module, "timezone"
    ):
        monkeypatch.setattr(request.module, "timezone", clock)

    original_pre_save = models.DateTimeField.pre_save

    def scheduling_timestamp(
        field: models.DateTimeField[datetime, datetime],
        instance: models.Model,
        *,
        add: bool,
    ) -> object:
        scheduling = instance._meta.app_label == "scheduling"
        if (scheduling or ambient is not None) and (
            field.auto_now or (field.auto_now_add and add)
        ):
            timestamp = reference if scheduling else authority_now
            setattr(instance, field.attname, timestamp)
            return timestamp
        result: object = original_pre_save(field, instance, add)
        return result

    monkeypatch.setattr(
        models.DateTimeField,
        "pre_save",
        lambda field, instance, add: scheduling_timestamp(field, instance, add=add),
    )
    with frozen_sql_clocks(database_url, reference, ambient) as authority_now:
        if ambient is not None:
            # Advance the actual Django ambient clock, not an unused test value.
            # Other domains and permission fixtures stay on the DB-time axis.
            authority = SimpleNamespace(**vars(timezone))
            authority.now = lambda: authority_now
            for name, module in tuple(sys.modules.items()):
                if (
                    (
                        name.startswith("apps.")
                        and not name.startswith("apps.scheduling.")
                    )
                    or name in authority_modules
                    or name == "identity.permission_support"
                ) and getattr(module, "timezone", None) is timezone:
                    monkeypatch.setattr(module, "timezone", authority)
            monkeypatch.setattr(timezone, "now", lambda: ambient)
            assert timezone.now() == ambient
        yield reference


@pytest.fixture
def resource_clock(controlled_scheduling_clock: datetime | None) -> datetime:
    assert controlled_scheduling_clock is not None
    return controlled_scheduling_clock
