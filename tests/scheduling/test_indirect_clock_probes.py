"""Catalog view edges and opaque extension leaves must not disappear."""

from __future__ import annotations

import psycopg
import pytest

from scheduling.clock_catalog import live_clock_inventory
from scheduling.test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

pytestmark = pytest.mark.django_db(transaction=True)

VIEW_PROBE = (
    "CREATE SCHEMA clock_probe; "
    "CREATE VIEW clock_probe.instant AS SELECT clock_timestamp() AS observed_at; "
    "CREATE FUNCTION clinic_app.scheduling_indirect_probe() RETURNS timestamptz "
    "LANGUAGE sql STABLE AS $$ "
    "SELECT (SELECT observed_at FROM clock_probe.instant) $$; "
    "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN indirect_clock "
    "timestamptz DEFAULT clinic_app.scheduling_indirect_probe()"
)
VIEW_REMOVE = (
    "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN indirect_clock; "
    "DROP FUNCTION clinic_app.scheduling_indirect_probe(); "
    "DROP VIEW clock_probe.instant; DROP SCHEMA clock_probe"
)
PROBES = {
    "view-stable": (VIEW_PROBE, VIEW_REMOVE),
    "view-immutable": (VIEW_PROBE.replace("STABLE", "IMMUTABLE"), VIEW_REMOVE),
    "view-search-path": (
        VIEW_PROBE.replace(
            "STABLE AS", "STABLE SET search_path=clock_probe,pg_catalog AS"
        ).replace("FROM clock_probe.instant", "FROM instant"),
        VIEW_REMOVE,
    ),
    "view-sql-standard": (
        VIEW_PROBE.replace("AS $$", "BEGIN ATOMIC").replace(" $$;", "; END;"),
        VIEW_REMOVE,
    ),
    "view-plpgsql-unresolved": (
        VIEW_PROBE.replace(
            "LANGUAGE sql STABLE AS $$ SELECT",
            "LANGUAGE plpgsql STABLE AS $$ BEGIN RETURN",
        ).replace(" $$;", "; END $$;"),
        VIEW_REMOVE,
    ),
    "materialized-view": (
        VIEW_PROBE.replace("CREATE VIEW", "CREATE MATERIALIZED VIEW"),
        VIEW_REMOVE.replace("DROP VIEW", "DROP MATERIALIZED VIEW"),
    ),
    "view-chain": (
        VIEW_PROBE.replace(
            "CREATE FUNCTION clinic_app.scheduling_indirect_probe()",
            "CREATE VIEW clock_probe.outer_view "
            "AS SELECT observed_at FROM clock_probe.instant; "
            "CREATE FUNCTION clinic_app.scheduling_indirect_probe()",
        ).replace(
            "(SELECT observed_at FROM clock_probe.instant)",
            "(SELECT observed_at FROM clock_probe.outer_view)",
        ),
        VIEW_REMOVE.replace(
            "DROP VIEW clock_probe.instant",
            "DROP VIEW clock_probe.outer_view; DROP VIEW clock_probe.instant",
        ),
    ),
    "extension-native": (
        "CREATE SCHEMA clock_probe; "
        "CREATE FUNCTION clock_probe.native() RETURNS timestamptz "
        "LANGUAGE internal STABLE AS 'now'; "
        "ALTER EXTENSION pgcrypto ADD FUNCTION clock_probe.native(); "
        "CREATE FUNCTION clinic_app.scheduling_indirect_probe() RETURNS timestamptz "
        "LANGUAGE sql STABLE AS $$ SELECT clock_probe.native() $$; "
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN indirect_clock "
        "timestamptz DEFAULT clinic_app.scheduling_indirect_probe()",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN indirect_clock; "
        "DROP FUNCTION clinic_app.scheduling_indirect_probe(); "
        "ALTER EXTENSION pgcrypto DROP FUNCTION clock_probe.native(); "
        "DROP FUNCTION clock_probe.native(); DROP SCHEMA clock_probe",
    ),
    "view-function-cycle": (
        "CREATE SCHEMA clock_probe; "
        "CREATE FUNCTION clock_probe.cycle(integer) RETURNS timestamptz "
        "LANGUAGE sql STABLE AS $$ SELECT NULL::timestamptz $$; "
        "CREATE VIEW clock_probe.instant AS SELECT clock_timestamp() AS observed_at, "
        "clock_probe.cycle(1) AS unused; "
        "CREATE OR REPLACE FUNCTION clock_probe.cycle(integer) RETURNS timestamptz "
        "LANGUAGE sql STABLE AS $$ "
        "SELECT CASE WHEN $1=0 THEN observed_at ELSE NULL END "
        "FROM clock_probe.instant $$; "
        "CREATE FUNCTION clinic_app.scheduling_indirect_probe() RETURNS timestamptz "
        "LANGUAGE sql STABLE AS $$ SELECT clock_probe.cycle(0) $$; "
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN indirect_clock "
        "timestamptz DEFAULT clinic_app.scheduling_indirect_probe()",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN indirect_clock; "
        "DROP FUNCTION clinic_app.scheduling_indirect_probe(); "
        "DROP VIEW clock_probe.instant; DROP FUNCTION clock_probe.cycle(integer); "
        "DROP SCHEMA clock_probe",
    ),
    "reader-operator": (
        "CREATE SCHEMA clock_probe; "
        "CREATE OPERATOR clock_probe.#@ (LEFTARG=timestamptz, RIGHTARG=interval, "
        "FUNCTION=pg_catalog.timestamptz_pl_interval); "
        "CREATE FUNCTION clinic_app.scheduling_indirect_probe() RETURNS timestamptz "
        "LANGUAGE sql STABLE AS $$ SELECT '2035-01-01 00:00:00+00'::timestamptz "
        "OPERATOR(clock_probe.#@) INTERVAL '1 hour' $$; "
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN indirect_clock "
        "timestamptz DEFAULT clinic_app.scheduling_indirect_probe()",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN indirect_clock; "
        "DROP FUNCTION clinic_app.scheduling_indirect_probe(); "
        "DROP OPERATOR clock_probe.#@(timestamptz, interval); DROP SCHEMA clock_probe",
    ),
}


@pytest.mark.parametrize(("install", "remove"), PROBES.values(), ids=PROBES)
def test_indirect_clock_reader_fails_inventory(
    superuser_database_url: str, install: str, remove: str
) -> None:
    before = live_clock_inventory()
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        owner.execute(install.encode())
        try:
            # The census parses as the migration owner. Exercise real catalog
            # edges, not the independent fail-closed permission-error fallback.
            owner.execute("GRANT USAGE ON SCHEMA clock_probe TO clinic_owner")
            owner.execute(
                "GRANT SELECT ON ALL TABLES IN SCHEMA clock_probe TO clinic_owner"
            )
            owner.execute(
                "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA clock_probe TO clinic_owner"
            )
            assert owner.execute(
                "SELECT clinic_app.scheduling_indirect_probe() IS NOT NULL"
            ).fetchone() == (True,)
            observed = live_clock_inventory()
            assert observed != before
            if "LANGUAGE plpgsql" not in install:
                assert not any(
                    key.startswith("unresolved-sql:")
                    for node in observed.values()
                    for key in node["direct"]
                )
            with pytest.raises(AssertionError):
                assert_inventory()
        finally:
            owner.execute(remove.encode())
    assert live_clock_inventory() == before
    assert_inventory()
