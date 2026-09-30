"""The census's declared denial contracts must cover its live SQL signatures."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from django.db import connection, transaction

from identity.legacy_guard_inventory import declared_probes
from identity.sql_denial_contracts import CONTRACTS, Declared, Shape, contract, derive
from identity.sql_guard_probes import PROBES
from identity.sql_probe_catalog import signature

if TYPE_CHECKING:
    from collections.abc import Callable


def test_contract_table_classifies_exactly_the_census_functions() -> None:
    functions = {
        name.split("#", 1)[0]
        for name in declared_probes()
        if name.startswith("clinic_app.")
    } | {probe.function for probe in PROBES.values()}
    assert sorted(CONTRACTS) == sorted(functions)


@pytest.mark.django_db
def test_declared_contracts_match_the_live_catalog(
    record_property: Callable[[str, object], None],
) -> None:
    derived = {}
    with connection.cursor() as cursor:
        for function, declared in CONTRACTS.items():
            found = contract(function, cursor)
            live = signature(function, cursor)
            derived[function] = {
                "shape": str(found.shape),
                "columns": [column.__name__ for column in found.columns],
                "arguments": live.arguments,
                "declared_return": live.declared_return,
                "denial": (
                    [found.refusal.sqlstate, found.refusal.message]
                    if found.refusal is not None
                    else [[value for _kind, value in row] for row in found.denial or ()]
                ),
            }
            if declared.refusal is None:
                continue
            cursor.execute(
                "SELECT pg_catalog.pg_get_functiondef(%s::pg_catalog.regproc)",
                [function],
            )
            ((definition,),) = cursor.fetchall()
            site = (
                f"RAISE EXCEPTION USING ERRCODE = '{declared.refusal.sqlstate}',"
                rf"\s+MESSAGE = '{re.escape(declared.refusal.message)}';"
            )
            assert re.search(site, definition), (function, "declared raise not found")
    record_property("sql_denial_contracts", json.dumps(derived, sort_keys=True))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("definition", "error"),
    [
        (
            "FUNCTION clinic_app.sql_contract_probe() RETURNS void"
            " LANGUAGE sql AS $$ SELECT $$",
            "unknown shape",
        ),
        (
            "FUNCTION clinic_app.sql_contract_probe() RETURNS record"
            " LANGUAGE sql AS $$ SELECT 1, 2 $$",
            "unknown shape",
        ),
        (
            "FUNCTION clinic_app.sql_contract_probe() RETURNS SETOF jsonb"
            " LANGUAGE sql AS $$ SELECT '{}'::jsonb $$",
            "result column types",
        ),
        (
            "FUNCTION clinic_app.sql_contract_probe(OUT a uuid, OUT b numeric)"
            " LANGUAGE sql AS $$ SELECT NULL::uuid, 1 $$",
            "result column types",
        ),
        (
            "PROCEDURE clinic_app.sql_contract_probe() LANGUAGE sql AS $$ SELECT $$",
            "not a plain function",
        ),
        (
            "FUNCTION clinic_app.sql_contract_probe(integer) RETURNS boolean"
            " LANGUAGE sql AS $$ SELECT false $$;"
            " CREATE FUNCTION clinic_app.sql_contract_probe(text) RETURNS boolean"
            " LANGUAGE sql AS $$ SELECT false $$",
            "not exactly one",
        ),
        (
            "FUNCTION clinic_app.sql_contract_probe(jsonb) RETURNS boolean"
            " LANGUAGE sql AS $$ SELECT false $$",
            "unknown argument types",
        ),
    ],
)
def test_unknown_function_shapes_fail_closed(
    definition: str, error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    function = "clinic_app.sql_contract_probe"
    monkeypatch.setitem(CONTRACTS, function, Declared(Shape.BOOLEAN))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"CREATE {definition}")
        with pytest.raises(AssertionError, match=error):
            derive(function, cursor)
        with pytest.raises(AssertionError, match=error):
            contract(function, cursor)
        transaction.set_rollback(True)


@pytest.mark.django_db
def test_out_and_inout_columns_preserve_the_input_signature() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION clinic_app.sql_contract_probe("
            "INOUT subject uuid, OUT label text) "
            "LANGUAGE sql AS $$ SELECT subject, NULL::text $$"
        )
        found = signature("clinic_app.sql_contract_probe", cursor)
    assert found.returns_set is False
    assert found.columns == (UUID, str)
    assert found.arguments == ("pg_catalog.uuid",)
    assert found.declared_return == "record"


@pytest.mark.django_db
def test_composite_boolean_column_is_not_a_boolean_return() -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TYPE clinic_app.sql_contract_row AS (decision boolean)")
        cursor.execute(
            "CREATE FUNCTION clinic_app.sql_contract_probe() "
            "RETURNS clinic_app.sql_contract_row "
            "LANGUAGE sql AS $$ SELECT NULL::boolean $$"
        )
        found = derive("clinic_app.sql_contract_probe", cursor)
    assert found.shape is Shape.SCALAR
    assert found.columns == (bool,)
    assert found.denial == (((type(None), None),),)
