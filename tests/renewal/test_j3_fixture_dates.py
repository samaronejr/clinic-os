"""Pin disjoint scheduling calendars for J3's shared practitioners."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, timedelta
from itertools import combinations
from unittest.mock import Mock

import pytest

from renewal.browser import test_agenda_grid, test_agenda_slice
from renewal.browser.test_journey_reception_pressure import (
    grid_world,
    reception_world,
    slice_world,
)

__all__ = ("agenda_staff", "grid_world", "reception_world", "slice_world")


@pytest.fixture(scope="session")
def agenda_staff() -> dict[str, str]:
    return {
        "organization": "synthetic",
        "clinic_a": "synthetic",
        "physician_a_id": "a",
        "physician_b_id": "b",
        "physician_long_id": "long",
    }


def test_j3_fixture_date_ranges_are_disjoint(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setattr(
        test_agenda_slice.SliceWorld, "_owner", lambda _: nullcontext(Mock())
    )
    # Hosted failure, and a slice shift onto J3's July 3 waitlist day.
    monkeypatch.setattr(test_agenda_grid, "RUN_SHIFT", timedelta(weeks=83))
    monkeypatch.setattr(test_agenda_slice, "RUN_SHIFT", timedelta(weeks=122))
    occupied = {
        "self-booking": {date(2035, 6, 5)},
        "waitlist": {date(2035, 7, 2), date(2035, 7, 3)},
    }
    for name, days in (("reception_world", 3), ("grid_world", 3), ("slice_world", 1)):
        world: test_agenda_slice.SliceWorld = request.getfixturevalue(name)
        occupied[name] = {
            world.next_day + timedelta(days=offset) for offset in range(days)
        }

    collisions = {
        (left, right): occupied[left] & occupied[right]
        for left, right in combinations(occupied, 2)
        if occupied[left] & occupied[right]
    }
    assert not collisions
