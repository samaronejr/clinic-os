"""Explicit scheduling time for the resource fixtures; audit/auth keep DB time."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.scheduling import (
    appointment_cancellation,
    appointment_rescheduling,
    appointment_values,
    availability_creation,
    availability_retirement,
    booking_queries,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def resource_clock(
    monkeypatch: pytest.MonkeyPatch, db: None, superuser_database_url: str
) -> Iterator[datetime]:
    """Freeze scheduling comparisons, including the two SQL scheduling clocks.

    No predicate is disabled: only its clock expression is injected. Restore the
    exact live definitions in finally; audit and session/role expiry clocks remain
    independent so their database-backed security checks are not bypassed.
    """
    reference = datetime(2035, 6, 1, tzinfo=UTC)
    for module in (
        availability_creation,
        availability_retirement,
        appointment_values,
        appointment_rescheduling,
        appointment_cancellation,
        booking_queries,
    ):
        monkeypatch.setattr(module, "timezone", SimpleNamespace(now=lambda: reference))
    originals: list[tuple[str, str]] = []
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        try:
            with owner.cursor() as cursor:
                for signature, expression in (
                    ("scheduling_appointment_guard_v1()", "CURRENT_TIMESTAMP"),
                    ("patient_booking_slots(date,uuid)", "statement_timestamp()"),
                ):
                    cursor.execute(
                        "SELECT pg_get_functiondef(%s::regprocedure)",
                        [f"clinic_app.{signature}"],
                    )
                    row = cursor.fetchone()
                    assert row is not None
                    original = str(row[0])
                    assert original.count(expression) == 1
                    cursor.execute(
                        original.replace(
                            expression, f"TIMESTAMPTZ '{reference.isoformat()}'"
                        )
                    )
                    originals.append((signature, original))
            yield reference
        finally:
            with owner.cursor() as cursor:
                for signature, original in reversed(originals):
                    cursor.execute(original)
                    cursor.execute(
                        "SELECT pg_get_functiondef(%s::regprocedure)",
                        [f"clinic_app.{signature}"],
                    )
                    assert cursor.fetchone() == (original,)
