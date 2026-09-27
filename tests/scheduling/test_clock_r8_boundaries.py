"""Score rule markers, not incidental fresh-root inventory differences."""

from __future__ import annotations

import json
import re

import pytest
from django.db import connection

from .clock_batch_probes import installed_family
from .clock_catalog import live_clock_inventory
from .clock_r8_probes import COMPOSITES, LITERALS, PL, cascade, expression
from .clock_residual_probes import Probe
from .test_residual_clock_probes import RESTORATION, installed_probe
from .test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


def rejected(request: pytest.FixtureRequest) -> None:
    with pytest.raises(AssertionError):
        assert_inventory()
    request.node.stash[RESTORATION]["census_rejected"] = "true"


@pytest.mark.parametrize("cases", [PL, COMPOSITES], ids=["plpgsql", "composites"])
def test_construction_class(
    cases: dict[str, Probe], superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    with installed_family(superuser_database_url, cases, request) as (observed, keys):
        scores = {}
        for name, key in keys.items():
            node = observed.get(key)
            direct = node["direct"] if node is not None else {}
            scores[name] = bool(
                direct.get("unresolved-temporal-coercion", 0)
                or any(marker.startswith("unanalysed-plpgsql:") for marker in direct)
            )
        request.node.stash[RESTORATION]["rule_cells"] = json.dumps(
            scores, sort_keys=True
        )
        assert set(scores) == set(cases)
        assert all(scores.values()), scores
        rejected(request)


def test_letter_boundaries(
    superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    cases = {
        value: expression(
            "SELECT '" + value + "'::timestamptz", result="::date=current_date"
        )
        for value in LITERALS
    }
    with installed_family(superuser_database_url, cases, request) as (observed, keys):
        scores = {}
        for name, key in keys.items():
            node = observed.get(key)
            scores[name] = (
                node["direct"].get("unresolved-temporal-input", 0) if node else 0
            )
        request.node.stash[RESTORATION]["literal_cells"] = json.dumps(
            scores, sort_keys=True
        )
        assert scores == dict.fromkeys(LITERALS, 1)
        rejected(request)


@pytest.mark.parametrize("action", ["CASCADE", "SET NULL", "SET DEFAULT"])
@pytest.mark.parametrize("event", ["DELETE", "UPDATE"])
def test_fk_actions(
    action: str, event: str, superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    with installed_probe(superuser_database_url, cascade(action, event), request):
        observed = live_clock_inventory()
        assert (
            observed["function:r8.stamp_del()"]["direct"][
                "pg_catalog.clock_timestamp()"
            ]
            == 1
        )
        assert (
            "trigger:r8.child.r8_stamp"
            in observed["function:clinic_app.scheduling_r8_root()"]["via"]
        )
        rejected(request)


def test_recursive_fk_actions(
    superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    with installed_probe(
        superuser_database_url, cascade("CASCADE", "DELETE", nested=True), request
    ):
        observed = live_clock_inventory()
        assert (
            "trigger:r8.child.r8_stamp"
            in observed["function:clinic_app.scheduling_r8_root()"]["via"]
        )
        assert "function:r8.stamp_del()" in observed["trigger:r8.child.r8_stamp"]["via"]
        rejected(request)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT NEW.kind INTO r8x;",
        "SELECT NEW.kind INTO STRICT r8x;",
        "SELECT (INTO r8x NEW.kind);",
        "FOR r8x IN SELECT NEW.kind LOOP NULL; END LOOP;",
    ],
)
def test_existing_node_assignments(
    statement: str, superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.scheduling_definition_guard()'::regprocedure)"
        )
        row = cursor.fetchone()
    assert row is not None
    original = str(row[0])
    injected = re.sub(
        r"\bBEGIN\b",
        "BEGIN DECLARE r8x clinic_app.scheduling_availabilityblock.end_at%TYPE; BEGIN "
        + statement
        + " END;",
        original,
        count=1,
        flags=re.IGNORECASE,
    )
    with installed_probe(
        superuser_database_url,
        Probe("R8-EXISTING", injected, original, "SELECT true"),
        request,
    ):
        direct = live_clock_inventory()[
            "function:clinic_app.scheduling_definition_guard()"
        ]["direct"]
        assert direct.get("unresolved-temporal-coercion", 0) or any(
            "unanalysed-plpgsql" in key for key in direct
        )
        rejected(request)
