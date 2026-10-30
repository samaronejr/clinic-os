"""Known assignment forms preserve typed inputs; SQL fallback is closed."""

from __future__ import annotations

from dataclasses import replace

import pytest

from .clock_catalog import live_clock_inventory
from .clock_plpgsql_statements import statements
from .clock_r8_probes import expression
from .test_residual_clock_probes import RESTORATION, installed_probe

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]

SAFE = {
    "into-strict": "SELECT p INTO STRICT x; RETURN x;",
    "for-query": "FOR x IN SELECT p LOOP RETURN x; END LOOP;",
    "foreach": "FOREACH x IN ARRAY ARRAY[p] LOOP RETURN x; END LOOP;",
    "fetch": "OPEN c FOR SELECT p; FETCH c INTO x; CLOSE c; RETURN x;",
    "return-next": "RETURN NEXT p;",
    "return-query": "RETURN QUERY SELECT p;",
}


@pytest.mark.parametrize("name", SAFE)
def test_temporal_statement_operands_are_proven(
    name: str, superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    probe = expression(
        "DECLARE p timestamptz:='2001-01-01'; x timestamptz; c refcursor; BEGIN "
        + SAFE[name]
        + " END",
        language="plpgsql",
        result="=TIMESTAMPTZ '2001-01-01'",
    )
    if name.startswith("return-"):
        probe = replace(
            probe,
            install=probe.install.replace(
                "RETURNS timestamptz LANGUAGE", "RETURNS SETOF timestamptz LANGUAGE"
            ),
        )
    with installed_probe(superuser_database_url, probe, request):
        node = live_clock_inventory().get("function:clinic_app.scheduling_r8_root()")
        direct = node["direct"] if node is not None else {}
        assert "unresolved-temporal-coercion" not in direct
        assert not any(key.startswith("unanalysed-plpgsql:") for key in direct)
        request.node.stash[RESTORATION]["typed_control"] = "true"


def test_typed_composite_domain_remains_supported(
    superuser_database_url: str, request: pytest.FixtureRequest
) -> None:
    probe = expression(
        "SELECT ((ROW(TIMESTAMPTZ '2001-01-01')::r8.rec)::r8.wrapper).at",
        ddl="CREATE TYPE r8.rec AS (at timestamptz); "
        "CREATE DOMAIN r8.wrapper AS r8.rec; ",
        result="=TIMESTAMPTZ '2001-01-01'",
    )
    with installed_probe(superuser_database_url, probe, request):
        assert not any(
            "unresolved-temporal-coercion" in node["direct"]
            for node in live_clock_inventory().values()
        )
        request.node.stash[RESTORATION]["typed_control"] = "true"


@pytest.mark.parametrize("source_type", ["timestamptz", "text"])
@pytest.mark.parametrize("alias", ["s", "AS s"])
def test_aliased_row_assignment_checks_each_temporal_field(
    source_type: str,
    alias: str,
    superuser_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    body = {
        "s": (
            "DECLARE item r8.destination; BEGIN SELECT * INTO item "
            "FROM r8.source s; RETURN item.at; END"
        ),
        "AS s": (
            "DECLARE item r8.destination; BEGIN SELECT * INTO item "
            "FROM r8.source AS s; RETURN item.at; END"
        ),
    }[alias]
    ddl = {
        "timestamptz": (
            "CREATE TYPE r8.destination AS (at timestamptz); "
            "CREATE TABLE r8.source(at timestamptz); "
            "INSERT INTO r8.source VALUES ('2001-01-01'); "
        ),
        "text": (
            "CREATE TYPE r8.destination AS (at timestamptz); "
            "CREATE TABLE r8.source(at text); "
            "INSERT INTO r8.source VALUES ('2001-01-01'); "
        ),
    }[source_type]
    probe = expression(
        body,
        ddl=ddl,
        language="plpgsql",
        result="=TIMESTAMPTZ '2001-01-01'",
    )
    with installed_probe(superuser_database_url, probe, request):
        node = live_clock_inventory().get("function:clinic_app.scheduling_r8_root()")
        direct = node["direct"] if node is not None else {}
        assert bool(direct.get("unresolved-temporal-coercion")) is (
            source_type == "text"
        )
        assert not any(key.startswith("unanalysed-plpgsql:") for key in direct)
        request.node.stash[RESTORATION]["typed_control"] = "true"


@pytest.mark.parametrize("source_type", ["timestamptz", "text"])
def test_select_into_checks_each_positional_temporal_target(
    source_type: str,
    superuser_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    probe = expression(
        f"DECLARE p {source_type}:='2001-01-01'; label text; x timestamptz; "
        "BEGIN SELECT 'Sintetico', p INTO label, x; RETURN x; END",
        language="plpgsql",
        result="=TIMESTAMPTZ '2001-01-01'",
    )
    with installed_probe(superuser_database_url, probe, request):
        direct = live_clock_inventory()["function:clinic_app.scheduling_r8_root()"][
            "direct"
        ]
        assert not any(key.startswith("unanalysed-plpgsql:") for key in direct)
        assert bool(direct.get("unresolved-temporal-coercion")) is (
            source_type == "text"
        )
        request.node.stash[RESTORATION]["positional_target_proven"] = "true"


@pytest.mark.parametrize(
    "statement",
    [
        "CALL p()",
        "EXECUTE 'SELECT 1'",
        "COMMIT",
        "ROLLBACK",
        "TRUNCATE t",
        "CREATE TABLE t(x integer)",
        "SET LOCAL application_name='synthetic'",
        "RESET application_name",
    ],
)
def test_unimplemented_grammar_is_never_assumed_inert(statement: str) -> None:
    found = statements("BEGIN " + statement + "; END;")
    assert len(found) == 1
    assert found[0].kind.startswith(("unanalysed:", "assignment"))
