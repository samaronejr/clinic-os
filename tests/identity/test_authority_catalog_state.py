"""Catalog hints and implicit objects cannot disappear from a certificate."""

from __future__ import annotations

import json
from typing import Literal, LiteralString, assert_never

import psycopg
import pytest
from django.db import connection, transaction
from psycopg import sql

from identity.authority_catalog import Catalog

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("hint", "object_sql"),
    [
        (
            "relhastriggers",
            "CREATE FUNCTION pg_temp.t17_hint_trigger() RETURNS trigger "
            "LANGUAGE plpgsql AS $body$ BEGIN RETURN NEW; END $body$; "
            "CREATE TRIGGER t17_hint BEFORE INSERT ON pg_temp.t17_hint "
            "FOR EACH ROW EXECUTE FUNCTION pg_temp.t17_hint_trigger()",
        ),
        ("relhasindex", "CREATE INDEX t17_hint_index ON pg_temp.t17_hint(value)"),
        (
            "relhasrules",
            "CREATE RULE t17_hint_rule AS ON INSERT TO pg_temp.t17_hint "
            "DO ALSO SELECT 1",
        ),
        (
            "relhassubclass",
            "CREATE TABLE clinic_app.t17_hint_child () INHERITS (pg_temp.t17_hint)",
        ),
    ],
)
def test_false_relation_hint_cannot_hide_actual_object(
    hint: str, object_sql: LiteralString, request: pytest.FixtureRequest
) -> None:
    database_url = request.getfixturevalue("superuser_database_url")
    assert isinstance(database_url, str)
    with psycopg.connect(database_url, autocommit=True) as admin:
        assert admin.execute(
            "SELECT to_regclass('clinic_app.t17_hint')"
        ).fetchone() == (None,)
        admin.execute("CREATE TABLE clinic_app.t17_hint (value integer)")
        try:
            # Committed owned test objects and hints are visible to the
            # certificate's separate backend; uncommitted temp objects are not.
            admin.execute(
                sql.SQL(
                    object_sql.replace("pg_temp.", "clinic_app.").replace(
                        "CREATE TEMP TABLE", "CREATE TABLE"
                    )
                )
            )
            admin.execute(
                sql.SQL(
                    "UPDATE pg_catalog.pg_class SET {} = false "
                    "WHERE oid = 'clinic_app.t17_hint'::regclass"
                ).format(sql.Identifier(hint))
            )
            with pytest.raises(AssertionError) as refused:
                Catalog().statement("SELECT value FROM clinic_app.t17_hint")
            assert refused.value.args[0] == (
                "relation_catalog_inconsistent",
                "clinic_app.t17_hint",
                "false_hint",
            )
        finally:
            admin.execute(
                sql.SQL(
                    "UPDATE pg_catalog.pg_class SET {} = true "
                    "WHERE oid = 'clinic_app.t17_hint'::regclass"
                ).format(sql.Identifier(hint))
            )
            if hint == "relhassubclass":
                admin.execute("DROP TABLE clinic_app.t17_hint_child")
            admin.execute("DROP TABLE clinic_app.t17_hint")
            if hint == "relhastriggers":
                admin.execute("DROP FUNCTION clinic_app.t17_hint_trigger()")


def test_internal_fk_functions_are_in_the_written_relation_closure() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE t17_fk_parent (id integer PRIMARY KEY)")
        cursor.execute(
            "CREATE TEMP TABLE t17_fk_child (parent_id integer "
            "REFERENCES pg_temp.t17_fk_parent(id))"
        )
        cursor.execute(
            "SELECT tgfoid FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = 'pg_temp.t17_fk_child'::regclass AND tgisinternal"
        )
        internal_functions = {row[0] for row in cursor.fetchall()}
        assert internal_functions
        written = Catalog().statement("INSERT INTO pg_temp.t17_fk_child VALUES (NULL)")
        assert internal_functions <= written.functions
        transaction.set_rollback(True)


def test_omitted_internal_fk_cannot_certify() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE t17_fk_omission_parent (id integer PRIMARY KEY); "
            "CREATE TEMP TABLE t17_fk_omission_child (parent_id integer "
            "REFERENCES pg_temp.t17_fk_omission_parent(id))"
        )
        cursor.execute("SELECT 'pg_temp.t17_fk_omission_child'::regclass::oid")
        row = cursor.fetchone()
        assert row is not None
        relation = row[0]
        catalog = Catalog()
        internal = [
            item
            for item in catalog.triggers
            if item.relation == relation and item.internal
        ]
        assert internal
        catalog.triggers = [item for item in catalog.triggers if item not in internal]
        with pytest.raises(AssertionError) as refused:
            catalog.statement("INSERT INTO pg_temp.t17_fk_omission_child VALUES (NULL)")
        assert refused.value.args[0] == (
            "relation_catalog_inconsistent",
            catalog.relations[relation].name,
            "object_population",
        )
        transaction.set_rollback(True)


def test_internal_native_alias_is_not_a_builtin_fk_exemption(
    request: pytest.FixtureRequest,
) -> None:
    database_url = request.getfixturevalue("superuser_database_url")
    assert isinstance(database_url, str)
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(
            "CREATE TEMP TABLE t17_alias_fk_parent (id integer PRIMARY KEY); "
            "CREATE TEMP TABLE t17_alias_fk_child (parent_id integer "
            "REFERENCES pg_temp.t17_alias_fk_parent(id)); "
            "CREATE FUNCTION pg_temp.t17_alias_fk_trigger() RETURNS trigger "
            "LANGUAGE internal AS 'RI_FKey_check_ins'; "
            "UPDATE pg_catalog.pg_trigger SET tgfoid="
            "'pg_temp.t17_alias_fk_trigger()'::regprocedure "
            "WHERE tgrelid='pg_temp.t17_alias_fk_child'::regclass AND tgisinternal"
        )
        namespace = admin.execute(
            "SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()"
        ).fetchone()
        assert namespace is not None
        text = (
            sql.SQL("INSERT INTO {} VALUES (NULL)")
            .format(sql.Identifier(namespace[0], "t17_alias_fk_child"))
            .as_string(admin)
        )
        catalog = Catalog()
        alias = next(
            function
            for function in catalog.functions.values()
            if function.name.endswith(".t17_alias_fk_trigger")
        )
        observed = catalog.statement(text)
        assert alias.oid in observed.functions
        assert observed.opaque == {"opaque function " + alias.name}


def test_index_expression_authority_is_in_the_read_closure() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE t17_index (value text)")
        cursor.execute(
            "CREATE FUNCTION pg_temp.t17_index_actor(text) RETURNS text "
            "LANGUAGE sql IMMUTABLE AS $body$ "
            "SELECT current_setting('app.current_user_id', true) $body$"
        )
        cursor.execute(
            "CREATE INDEX t17_index_actor ON pg_temp.t17_index "
            "(pg_temp.t17_index_actor(value))"
        )
        read = Catalog().statement("SELECT value FROM pg_temp.t17_index")
        assert "app.current_user_id" in read.settings
        transaction.set_rollback(True)


@pytest.mark.parametrize("object_kind", ["triggers", "indexes", "rules", "children"])
def test_omitted_catalog_object_cannot_certify(
    object_kind: Literal["triggers", "indexes", "rules", "children"],
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE t17_omission (value integer PRIMARY KEY); "
            "CREATE TEMP TABLE t17_omission_child () "
            "INHERITS (pg_temp.t17_omission); "
            "CREATE FUNCTION pg_temp.t17_omission_trigger() RETURNS trigger "
            "LANGUAGE plpgsql AS $body$ BEGIN RETURN NEW; END $body$; "
            "CREATE TRIGGER t17_omission BEFORE INSERT ON pg_temp.t17_omission "
            "FOR EACH ROW EXECUTE FUNCTION pg_temp.t17_omission_trigger(); "
            "CREATE RULE t17_omission_rule AS ON INSERT TO pg_temp.t17_omission "
            "DO ALSO SELECT 1"
        )
        cursor.execute("SELECT 'pg_temp.t17_omission'::regclass::oid")
        row = cursor.fetchone()
        assert row is not None
        relation = row[0]
        catalog = Catalog()
        catalog.statement("SELECT value FROM pg_temp.t17_omission")
        match object_kind:
            case "triggers":
                catalog.triggers = [
                    item for item in catalog.triggers if item.relation != relation
                ]
            case "indexes":
                catalog.indexes = [
                    item for item in catalog.indexes if item[0] != relation
                ]
            case "rules":
                catalog.rules = [item for item in catalog.rules if item[0] != relation]
            case "children":
                catalog.children = [
                    item for item in catalog.children if item[0] != relation
                ]
            case unexpected:
                assert_never(unexpected)
        with pytest.raises(AssertionError) as refused:
            catalog.statement("SELECT value FROM pg_temp.t17_omission")
        assert refused.value.args[0] == (
            "relation_catalog_inconsistent",
            catalog.relations[relation].name,
            "object_population",
        )
        transaction.set_rollback(True)


@pytest.mark.parametrize(
    "field",
    ["indisvalid", "indisready", "indisunique", "indexprs", "indpred"],
)
def test_changed_index_state_cannot_certify(field: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE t17_index_state (value text); "
            "CREATE UNIQUE INDEX t17_index_state_unique "
            "ON pg_temp.t17_index_state(lower(value)) WHERE value IS NOT NULL"
        )
        catalog = Catalog()
        captured = next(
            item
            for item in catalog.indexes
            if catalog.relations.get(item[0]) is not None
            and catalog.relations[item[0]].name.endswith(".t17_index_state")
        )
        relation, state, expression, predicate = captured
        cursor.execute(
            "SELECT to_jsonb(i)->%s FROM pg_index i "
            "WHERE indexrelid='pg_temp.t17_index_state_unique'::regclass",
            [field],
        )
        expected = cursor.fetchone()
        assert expected is not None
        assert isinstance(expected[0], str)
        altered = json.loads(state)
        assert altered[0][field] == json.loads(expected[0])
        altered[0][field] = None if field in {"indexprs", "indpred"} else False
        changed = (relation, json.dumps(altered), expression, predicate)
        catalog.indexes = [
            changed if item == captured else item for item in catalog.indexes
        ]
        with pytest.raises(AssertionError) as refused:
            catalog.statement("SELECT value FROM pg_temp.t17_index_state")
        assert refused.value.args[0] == (
            "relation_catalog_inconsistent",
            catalog.relations[relation].name,
            "object_population",
        )
        transaction.set_rollback(True)


@pytest.mark.parametrize("field", ["expression", "predicate"])
def test_omitted_index_expression_cannot_certify(
    field: Literal["expression", "predicate"],
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE t17_index_expression (value text); "
            "CREATE INDEX t17_index_expression_lower "
            "ON pg_temp.t17_index_expression(lower(value)) WHERE value IS NOT NULL"
        )
        catalog = Catalog()
        captured = next(
            item
            for item in catalog.indexes
            if catalog.relations.get(item[0]) is not None
            and catalog.relations[item[0]].name.endswith(".t17_index_expression")
        )
        relation, state, expression, predicate = captured
        catalog.indexes = [
            (
                relation,
                state,
                None if field == "expression" else expression,
                None if field == "predicate" else predicate,
            )
            if item == captured
            else item
            for item in catalog.indexes
        ]
        with pytest.raises(AssertionError) as refused:
            catalog.statement("SELECT value FROM pg_temp.t17_index_expression")
        assert refused.value.args[0][0] == "index_expression_binding_mismatch"
        transaction.set_rollback(True)
