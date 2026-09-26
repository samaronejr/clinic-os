"""Exact clock census over migration sources and installed scheduling functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.db import connection

from scheduling.clock_catalog import catalog_readers, functions, live_clock_inventory
from scheduling.clock_support import SOURCE_CLOCKS, SQL_CLOCKS, clock_counts

pytestmark = pytest.mark.django_db(transaction=True)


def test_all_migration_sql_time_reads_are_accounted_for() -> None:
    migrations = Path(__file__).resolve().parents[2] / "apps/scheduling/migrations"
    observed = {
        path.name: dict(counts)
        for path in migrations.glob("*.py")
        if (counts := clock_counts(path.read_text()))
    }
    assert observed == SOURCE_CLOCKS


def test_all_live_scheduling_sql_time_reads_are_controlled() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname || '(' || oidvectortypes(p.proargtypes) || ')', "
            "p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='clinic_app' AND (p.proname LIKE 'scheduling_%%' "
            "OR p.proname LIKE 'patient_booking_%%' OR p.proname LIKE 'waitlist_%%')"
        )
        observed = {
            signature.replace(" ", ""): dict(counts)
            for signature, source in cursor.fetchall()
            if (counts := clock_counts(source))
        }
    assert observed == SQL_CLOCKS
    expected = json.loads(
        Path(__file__).with_name("clock_catalog_inventory.json").read_text()
    )
    assert live_clock_inventory() == expected


def test_catalog_reader_set_contains_the_explicit_clock_cross_check() -> None:
    procedures = functions()
    readers = catalog_readers()
    names = {procedures[oid].name for oid in readers}
    assert {"now", "statement_timestamp", "clock_timestamp"} <= names
    assert {"transaction_timestamp", "timeofday"} <= names
    for oid in readers:
        reader = procedures[oid]
        assert reader.schema == "pg_catalog"
        assert reader.volatility in {"s", "v"}
        assert reader.returns in {
            "timestamp with time zone",
            "timestamp without time zone",
            "date",
            "time with time zone",
            "time without time zone",
        } or (reader.name == "timeofday" and reader.returns == "text")


@pytest.mark.parametrize(
    ("expression", "normalized"),
    [
        ("pg_catalog.statement_timestamp ()", "statement_timestamp()"),
        ("NOW()", "now()"),
        ("CURRENT_DATE", "current_date"),
        ("CURRENT_TIMESTAMP", "current_timestamp"),
        ("CURRENT_TIMESTAMP(3)", "current_timestamp(3)"),
        ("clock_timestamp()", "clock_timestamp()"),
        ("LOCALTIMESTAMP", "localtimestamp"),
        ("localtimestamp(6)", "localtimestamp(6)"),
    ],
)
def test_clock_census_recognizes_every_sql_spelling(
    expression: str, normalized: str
) -> None:
    assert clock_counts(f"SELECT {expression}") == {normalized: 1}
