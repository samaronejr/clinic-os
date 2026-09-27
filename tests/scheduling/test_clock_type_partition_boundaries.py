"""Reproduce round-7 escapes on PostgreSQL and reject by the intended rule."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from psycopg import sql

from .clock_catalog import Census, catalog_readers, functions, live_clock_inventory
from .clock_type_partition_probes import (
    CONSTRUCTIONS,
    DOMAIN_DEFAULT,
    DOMAINS,
    INHERITANCE,
    LITERALS,
    PARTITIONS,
    expression_probe,
)
from .clock_types import Types
from .test_residual_clock_probes import RESTORATION, installed_probe
from .test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

if TYPE_CHECKING:
    from .clock_residual_probes import Probe

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


def test_catalog_field_spellings_preserve_exact_type_resolution() -> None:
    types = Types(catalog_readers())
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT n.nspname,c.relname,a.attname,a.atttypid FROM pg_attribute a "
            "JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE a.attnum>0 AND NOT a.attisdropped"
        )
        for schema, table, field, type_oid in cursor.fetchall():
            spelling = sql.Identifier(schema, table, field).as_string() + "%TYPE"
            assert types.resolve(spelling) == type_oid


def test_relation_surface_reads_are_batched_before_graph_walk() -> None:
    census = Census(functions(), catalog_readers())
    with CaptureQueriesContext(connection) as queries:
        for relation in census.batched_surfaces:
            census.related_surfaces(relation)
    assert queries.captured_queries == []


def rejected(request: pytest.FixtureRequest) -> None:
    with pytest.raises(AssertionError):
        assert_inventory()
    request.node.stash[RESTORATION]["census_rejected"] = "true"


@pytest.mark.parametrize("name", DOMAINS)
def test_domain_surfaces_are_reached(
    superuser_database_url: str, name: str, request: pytest.FixtureRequest
) -> None:
    with installed_probe(superuser_database_url, DOMAINS[name], request) as owner:
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute("SELECT clinic_app.scheduling_r7_root('2999-01-01')")
        observed = live_clock_inventory()
        assert observed["domain-constraint:r7.fresh.clock_check"]["direct"] == {
            "pg_catalog.now()": 1,
        }
        rejected(request)


def test_domain_default_is_reached(
    superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    with installed_probe(superuser_database_url, DOMAIN_DEFAULT, request):
        observed = live_clock_inventory()
        assert observed["domain-default:r7.fresh"]["direct"] == {"pg_catalog.now()": 1}
        rejected(request)


@pytest.mark.parametrize("name", PARTITIONS)
def test_partition_trigger_is_reached_through_parent(
    superuser_database_url: str, name: str, request: pytest.FixtureRequest
) -> None:
    with installed_probe(superuser_database_url, PARTITIONS[name], request):
        observed = live_clock_inventory()
        assert (
            "trigger:r7.pt1.r7_stamp"
            in observed["function:clinic_app.scheduling_r7_root()"]["via"]
        )
        assert (
            observed["function:r7.stamp()"]["direct"]["pg_catalog.clock_timestamp()"]
            == 1
        )
        rejected(request)


def test_inheritance_children_keep_every_relation_surface(
    superuser_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    with installed_probe(superuser_database_url, INHERITANCE, request):
        observed = live_clock_inventory()
        expected = {
            "default:r7.child.at",
            "constraint:r7.child.child_clock",
            "policy:r7.child.child_clock",
            "rule:r7.child.child_clock",
            "trigger:r7.child.child_stamp",
        }
        assert expected <= set(
            observed["function:clinic_app.scheduling_r7_root()"]["via"]
        )
        rejected(request)


@pytest.mark.parametrize("value", LITERALS)
def test_clock_token_anywhere_in_literal(
    superuser_database_url: str, value: str, request: pytest.FixtureRequest
) -> None:
    expected = "current_date::timestamptz"
    if value == "10:00 today":
        expected = "current_date + TIME '10:00'"
    elif value in {",now", "(now)"}:
        expected = "now()"
    probe = replace(
        expression_probe("sql", "", "SELECT '" + value + "'::timestamptz"),
        runtime="SELECT clinic_app.scheduling_r7_root() = " + expected,
    )
    with installed_probe(superuser_database_url, probe, request):
        observed = live_clock_inventory()
        assert (
            observed["function:clinic_app.scheduling_r7_root()"]["direct"][
                "unresolved-temporal-input"
            ]
            == 1
        )
        rejected(request)


@pytest.mark.parametrize("name", CONSTRUCTIONS)
def test_nonliteral_text_to_temporal_is_refused(
    superuser_database_url: str, name: str, request: pytest.FixtureRequest
) -> None:
    language, arguments, body = CONSTRUCTIONS[name]
    probe = expression_probe(language, arguments, body)
    with installed_probe(superuser_database_url, probe, request):
        observed = live_clock_inventory()
        key = (
            "function:clinic_app.scheduling_r7_root("
            + ("text" if arguments else "")
            + ")"
        )
        assert observed[key]["direct"]["unresolved-temporal-coercion"] >= 1
        rejected(request)


@pytest.mark.parametrize(
    "body", ["SELECT p::timestamp::timestamptz", "BEGIN RETURN p; END"]
)
def test_nontext_temporal_operands_remain_supported(
    superuser_database_url: str, body: str, request: pytest.FixtureRequest
) -> None:
    language = "sql" if body.startswith("SELECT") else "plpgsql"
    probe: Probe = replace(
        expression_probe(language, "p timestamptz", body),
        remove="DROP FUNCTION clinic_app.scheduling_r7_root(timestamptz)",
        runtime="SELECT clinic_app.scheduling_r7_root('2001-01-01') "
        "= TIMESTAMPTZ '2001-01-01'",
    )
    with installed_probe(superuser_database_url, probe, request):
        observed = live_clock_inventory()
        assert all(
            "unresolved-temporal-coercion" not in node["direct"]
            for node in observed.values()
        )
