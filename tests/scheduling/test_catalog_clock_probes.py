"""Real-catalog negative probes, restored without changing production SQL."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest

from scheduling.clock_catalog import live_clock_inventory
from scheduling.test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

if TYPE_CHECKING:
    from typing import LiteralString

pytestmark = pytest.mark.django_db(transaction=True)

PROBES = {
    "transaction-function": (
        "CREATE FUNCTION clinic_app.scheduling_gate_clock_probe() "
        "RETURNS timestamptz LANGUAGE sql STABLE "
        "AS $$ SELECT transaction_timestamp() $$",
        "DROP FUNCTION clinic_app.scheduling_gate_clock_probe()",
    ),
    "column-default": (
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN clock_probe "
        "timestamptz DEFAULT now() + interval '1 hour'",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN clock_probe",
    ),
    "policy": (
        "CREATE POLICY clock_probe ON clinic_app.scheduling_resource "
        "AS RESTRICTIVE FOR SELECT TO clinic_app "
        "USING (transaction_timestamp() > TIMESTAMPTZ '2000-01-01')",
        "DROP POLICY clock_probe ON clinic_app.scheduling_resource",
    ),
    "policy-with-check": (
        "CREATE POLICY clock_probe ON clinic_app.scheduling_resource "
        "AS RESTRICTIVE FOR ALL TO clinic_app USING (true) "
        "WITH CHECK (clock_timestamp() > TIMESTAMPTZ '2000-01-01')",
        "DROP POLICY clock_probe ON clinic_app.scheduling_resource",
    ),
    "check": (
        "ALTER TABLE clinic_app.scheduling_resource ADD CONSTRAINT clock_probe "
        "CHECK (transaction_timestamp() > TIMESTAMPTZ '2000-01-01') NOT VALID",
        "ALTER TABLE clinic_app.scheduling_resource DROP CONSTRAINT clock_probe",
    ),
    "view": (
        "CREATE VIEW clinic_app.scheduling_clock_probe AS SELECT CURRENT_TIME AS value",
        "DROP VIEW clinic_app.scheduling_clock_probe",
    ),
    "materialized-view": (
        "CREATE MATERIALIZED VIEW clinic_app.scheduling_clock_probe AS "
        "SELECT timeofday() AS value WITH NO DATA",
        "DROP MATERIALIZED VIEW clinic_app.scheduling_clock_probe",
    ),
    "dependent-view": (
        "CREATE VIEW clinic_app.clock_probe AS SELECT transaction_timestamp() AS value "
        "FROM clinic_app.scheduling_resource",
        "DROP VIEW clinic_app.clock_probe",
    ),
    "generated": (
        "CREATE FUNCTION clinic_app.clock_probe() RETURNS timestamptz "
        "LANGUAGE sql IMMUTABLE AS $$ SELECT transaction_timestamp() $$; "
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN clock_probe "
        "timestamptz GENERATED ALWAYS AS (clinic_app.clock_probe()) STORED",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN clock_probe; "
        "DROP FUNCTION clinic_app.clock_probe()",
    ),
    "trigger-when": (
        "CREATE FUNCTION clinic_app.clock_probe() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RETURN NEW; END $$; CREATE TRIGGER clock_probe BEFORE UPDATE "
        "ON clinic_app.scheduling_resource FOR EACH ROW "
        "WHEN (transaction_timestamp() > TIMESTAMPTZ '2000-01-01') "
        "EXECUTE FUNCTION clinic_app.clock_probe()",
        "DROP TRIGGER clock_probe ON clinic_app.scheduling_resource; "
        "DROP FUNCTION clinic_app.clock_probe()",
    ),
    "transitive-cycle": (
        "CREATE SCHEMA clock_probe; CREATE FUNCTION clock_probe.leaf() "
        "RETURNS timestamptz LANGUAGE sql STABLE "
        "AS $$ SELECT transaction_timestamp() $$; "
        "CREATE FUNCTION clock_probe.bridge() RETURNS timestamptz LANGUAGE plpgsql "
        "IMMUTABLE AS $$ BEGIN IF false THEN RETURN clock_probe.cycle(); END IF; "
        "RETURN clock_probe.leaf(); END $$; "
        "CREATE FUNCTION clock_probe.cycle() RETURNS timestamptz LANGUAGE sql "
        "AS $$ SELECT clock_probe.bridge() $$; "
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN clock_probe "
        "timestamptz DEFAULT clock_probe.bridge()",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN clock_probe; "
        "DROP FUNCTION clock_probe.cycle(); DROP FUNCTION clock_probe.bridge(); "
        "DROP FUNCTION clock_probe.leaf(); DROP SCHEMA clock_probe",
    ),
    "domain-default": (
        "CREATE DOMAIN clinic_app.clock_probe AS timestamptz DEFAULT now(); "
        "ALTER TABLE clinic_app.scheduling_resource ADD COLUMN clock_probe "
        "clinic_app.clock_probe",
        "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN clock_probe; "
        "DROP DOMAIN clinic_app.clock_probe",
    ),
    "inherited-default": (
        "CREATE TABLE clinic_app.clock_probe "
        "(clock_probe timestamptz DEFAULT transaction_timestamp()) "
        "INHERITS (clinic_app.scheduling_resource)",
        "DROP TABLE clinic_app.clock_probe",
    ),
    "index-expression": (
        "CREATE FUNCTION clinic_app.clock_probe() RETURNS timestamptz LANGUAGE sql "
        "IMMUTABLE AS $$ SELECT transaction_timestamp() $$; "
        "CREATE INDEX clock_probe ON clinic_app.scheduling_resource "
        "((clinic_app.clock_probe()))",
        "DROP INDEX clinic_app.clock_probe; DROP FUNCTION clinic_app.clock_probe()",
    ),
}


@pytest.mark.parametrize(("install", "remove"), PROBES.values(), ids=PROBES)
def test_live_census_rejects_added_clock_reader(
    superuser_database_url: str, install: LiteralString, remove: LiteralString
) -> None:
    before = live_clock_inventory()
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        owner.execute(install)
        try:
            assert live_clock_inventory() != before
            with pytest.raises(AssertionError):
                assert_inventory()
        finally:
            owner.execute(remove)
    assert live_clock_inventory() == before
    assert_inventory()
