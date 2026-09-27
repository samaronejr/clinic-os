"""Refresh session surfaces and reject objects the server cannot analyse."""

import psycopg
import pytest
from django.db import connection

from scheduling.clock_catalog import live_clock_inventory
from scheduling.test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("prepared", [True, False], ids=["prepared", "pg-temp"])
def test_session_objects_are_reclassified_per_execution(*, prepared: bool) -> None:
    before = live_clock_inventory()
    with connection.cursor() as cursor:
        if prepared:
            cursor.execute(
                "PREPARE clock_probe AS SELECT transaction_timestamp() IS NOT NULL"
            )
        else:
            cursor.execute(
                "CREATE TEMP TABLE clock_probe (at timestamptz DEFAULT now())"
            )
        try:
            if prepared:
                cursor.execute("EXECUTE clock_probe")
            else:
                cursor.execute(
                    "INSERT INTO pg_temp.clock_probe DEFAULT VALUES "
                    "RETURNING at IS NOT NULL"
                )
            assert cursor.fetchone() == (True,)
            observed = live_clock_inventory()
            assert observed != before
            if prepared:
                assert (
                    observed["prepared:clock_probe"]["direct"][
                        "pg_catalog.transaction_timestamp()"
                    ]
                    == 1
                )
            with pytest.raises(AssertionError):
                assert_inventory()
        finally:
            cursor.execute(
                "DEALLOCATE clock_probe"
                if prepared
                else "DROP TABLE pg_temp.clock_probe"
            )
    assert live_clock_inventory() == before
    assert_inventory()


def test_unresolvable_sql_body_is_not_clock_free(superuser_database_url: str) -> None:
    before = live_clock_inventory()
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        owner.execute(
            "CREATE FUNCTION clinic_app.scheduling_polymorphic_probe(anyelement) "
            "RETURNS anyelement LANGUAGE sql IMMUTABLE AS $$ SELECT $1 $$"
        )
        try:
            assert owner.execute(
                "SELECT clinic_app.scheduling_polymorphic_probe(42)"
            ).fetchone() == (42,)
            observed = live_clock_inventory()
            assert any(
                key.startswith("unresolved-sql:")
                for node in observed.values()
                for key in node["direct"]
            )
            assert observed != before
            with pytest.raises(AssertionError):
                assert_inventory()
        finally:
            owner.execute(
                "DROP FUNCTION clinic_app.scheduling_polymorphic_probe(anyelement)"
            )
    assert live_clock_inventory() == before
    assert_inventory()


def test_opaque_review_pins_extension_membership(superuser_database_url: str) -> None:
    before = live_clock_inventory()
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        owner.execute(
            "ALTER EXTENSION pgcrypto DROP FUNCTION clinic_app.gen_random_uuid()"
        )
        try:
            assert owner.execute(
                "SELECT clinic_app.gen_random_uuid() IS NOT NULL"
            ).fetchone() == (True,)
            observed = live_clock_inventory()
            assert observed["function:clinic_app.gen_random_uuid()"]["direct"] == {
                "opaque:unreviewed:clinic_app.gen_random_uuid()": 1,
            }
            assert observed != before
            with pytest.raises(AssertionError):
                assert_inventory()
        finally:
            owner.execute(
                "ALTER EXTENSION pgcrypto ADD FUNCTION clinic_app.gen_random_uuid()"
            )
    assert live_clock_inventory() == before
    assert_inventory()
