"""Regression for the hosted J3 practitioner availability collision."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import Mock

import pytest

from renewal.browser import test_agenda_grid, test_agenda_slice
from renewal.browser.test_journey_reception_pressure import grid_world, reception_world

__all__ = ("agenda_staff", "grid_world", "reception_world")


@pytest.fixture(scope="session")
def agenda_staff() -> dict[str, str]:
    return {
        "organization": "synthetic",
        "clinic_a": "synthetic",
        "physician_a_id": "a",
        "physician_b_id": "b",
        "physician_long_id": "long",
    }


def test_j3_grid_dates_do_not_overlap_reception(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setattr(
        test_agenda_slice.SliceWorld, "_owner", lambda _: nullcontext(Mock())
    )
    # The hosted shift started the grid on April 7, inside reception's April 6-8.
    monkeypatch.setattr(test_agenda_grid, "RUN_SHIFT", timedelta(weeks=83))
    grid: test_agenda_grid.GridWorld = request.getfixturevalue("grid_world")
    reception: test_agenda_slice.SliceWorld = request.getfixturevalue("reception_world")
    grid_days = {grid.next_day + timedelta(days=offset) for offset in range(3)}
    reception_days = {
        reception.next_day + timedelta(days=offset) for offset in range(3)
    }
    assert not grid_days & reception_days
