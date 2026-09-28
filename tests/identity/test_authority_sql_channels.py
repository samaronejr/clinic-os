"""Views, string bodies, native code and alternate SQL paths fail closed."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from django.db import connection, transaction

from identity.authority_catalog import Catalog, Reads
from identity.authority_observer import AuthorityObservedError, AuthorityObserver
from identity.authority_sql import references
from identity.nonstaff_differential import (
    DifferentialProbe,
    _invoke,
    assert_behavioral_classifications,
)
from identity.nonstaff_states import ReplayScope

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _query(statement: str) -> object:
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()


def _raw_query() -> bool:
    assert connection.connection is not None
    connection.connection.pgconn.exec_(b"SELECT current_setting('app.current_user_id')")
    return True


def _cached_query(execute: Callable[[bytes], object]) -> bool:
    execute(b"SELECT current_setting('app.current_user_id')")
    return True


def test_prebound_native_method_is_observed(rbac_graph: RbacGraph) -> None:
    actor = User.objects.create(username="synthetic-cached-sql-" + uuid4().hex)
    assert connection.connection is not None
    execute = connection.connection.pgconn.exec_
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    probe = DifferentialProbe(
        __name__ + "._cached_query", lambda: _cached_query(execute)
    )
    assert _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
    with pytest.raises(AuthorityObservedError):
        observer.assert_nonstaff(probe.symbol)
    assert ("opaque", "SQL bypassed the observed cursor") in observer.touches


def _observe(graph: RbacGraph, query: str) -> AuthorityObserver:
    actor = User.objects.create(username="synthetic-sql-observer-" + uuid4().hex)
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    probe = DifferentialProbe(__name__ + "._query", lambda: _query(query))
    assert _invoke(probe, actor, graph.organization_a, authority=observer)
    with pytest.raises(AuthorityObservedError):
        observer.assert_nonstaff(probe.symbol)
    return observer


@pytest.mark.parametrize(
    ("statement", "setting"),
    [
        ("SHOW app.current_user_id", "app.current_user_id"),
        ("SHOW app.current_tenant", "app.current_tenant"),
        ('sHoW /* probe */ app."current_user_id";', "app.current_user_id"),
        ('SHOW "APP.CURRENT_USER_ID"', "app.current_user_id"),
        ("SELECT current_setting('app.current_user_id', true)", "app.current_user_id"),
        ("SELECT current_setting('APP.CURRENT_USER_ID', true)", "app.current_user_id"),
        ("SELECT set_config('app.current_user_id', NULL, true)", "app.current_user_id"),
        ("SELECT set_config('APP.CURRENT_USER_ID', NULL, true)", "app.current_user_id"),
        (
            "SELECT set_config('app.observer_copy', "
            "current_setting('app.current_user_id'), true)",
            "app.current_user_id",
        ),
    ],
)
def test_setting_reader_forms_are_observed(
    rbac_graph: RbacGraph, statement: str, setting: str
) -> None:
    observer = _observe(rbac_graph, statement)
    assert ("guc", setting) in observer.touches


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("SHOW ALL", "unresolved SHOW setting"),
        (
            "SELECT current_setting(concat('app.current_', 'user_id'), true)",
            "computed setting name",
        ),
        (
            "SELECT set_config(concat('app.current_', 'user_id'), NULL, true)",
            "computed setting name",
        ),
    ],
)
def test_unresolved_setting_reads_are_opaque(
    rbac_graph: RbacGraph, statement: str, reason: str
) -> None:
    observer = _observe(rbac_graph, statement)
    assert ("opaque", reason) in observer.touches


@pytest.mark.parametrize("statement", ["SHOW %s", "SHOW app.", "SHOW 'setting'"])
def test_unresolved_show_name_fails_closed(statement: str) -> None:
    assert "unresolved SHOW setting" in references(statement).opaque


def test_literal_nonactor_setting_is_resolved() -> None:
    parsed = references("SHOW statement_timeout;")
    assert parsed.settings == {"statement_timeout"}
    assert not parsed.opaque


def test_show_guard_cannot_claim_nonstaff(rbac_graph: RbacGraph) -> None:
    actor = User.objects.create(username="synthetic-show-guard-" + uuid4().hex)
    probe = DifferentialProbe(
        __name__ + "._query", lambda: _query("SHOW app.current_user_id"), bool
    )
    with pytest.raises(AuthorityObservedError) as refused:
        assert_behavioral_classifications(
            [{"symbol": probe.symbol, "kind": "nonstaff", "signals": []}],
            {probe.symbol: [probe]},
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )
    assert ("guc", "app.current_user_id") in refused.value.args[1]


def test_empty_relation_is_observed_by_counters_without_text_classifier(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bypass only the metadata classifier, not SQL execution or its outcomes.
    # A missing relation-edge classifier must not blind the independent counters.
    monkeypatch.setattr(Catalog, "statement", lambda *_args, **_kwargs: Reads())
    observer = _observe(
        rbac_graph, "SELECT count(*) FROM clinic_app.identity_userclinicrole"
    )
    assert ("relation", "clinic_app.identity_userclinicrole") in observer.touches


def test_view_reader_uses_pg_rewrite_dependencies(rbac_graph: RbacGraph) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP VIEW t6_authority_view AS "
            "SELECT * FROM clinic_app.identity_rolegrant"
        )
        cursor.execute("GRANT SELECT ON t6_authority_view TO clinic_app")
    observer = _observe(rbac_graph, "SELECT count(*) FROM pg_temp.t6_authority_view")
    assert ("relation", "clinic_app.identity_rolegrant") in observer.touches


def test_view_setting_read_is_parsed_from_the_view_definition(
    rbac_graph: RbacGraph,
) -> None:
    # pg_rewrite dependencies name current_setting but not the setting; only
    # the view's definition text shows which GUC it reads.
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP VIEW t6_actor_view AS "
            "SELECT current_setting('app.current_user_id', true) AS actor"
        )
        cursor.execute("GRANT SELECT ON t6_actor_view TO clinic_app")
    observer = _observe(rbac_graph, "SELECT actor FROM pg_temp.t6_actor_view")
    assert ("guc", "app.current_user_id") in observer.touches


# Pure parser cases touch no rows: the closest mark overrides the module's
# transaction=True, so each case rolls back instead of flushing the database.
PARSER_ONLY = pytest.mark.django_db


@PARSER_ONLY
@pytest.mark.parametrize(
    ("statement", "setting"),
    [
        ("SET LOCAL app.current_user_id = ''", "app.current_user_id"),
        ("SET app.current_user_id TO DEFAULT", "app.current_user_id"),
        ("SET SESSION app.current_tenant = 'x'", "app.current_tenant"),
        ("RESET app.current_user_id", "app.current_user_id"),
        ("SET app.current_user_id FROM CURRENT", "app.current_user_id"),
        ('SET "app"."current_user_id" = 1', "app.current_user_id"),
        (
            "BEGIN IF a THEN SET LOCAL app.current_user_id = ''; END IF; END",
            "app.current_user_id",
        ),
        ("BEGIN <<l>> RESET app.current_user_id; END", "app.current_user_id"),
    ],
)
def test_set_and_reset_statements_record_the_setting(
    statement: str, setting: str
) -> None:
    parsed = references(statement)
    assert setting in parsed.settings
    assert not parsed.opaque


@PARSER_ONLY
@pytest.mark.parametrize(
    "statement",
    [
        "RESET ALL",
        "RESET ROLE",
        "SET ROLE clinic_owner",
        "SET SESSION AUTHORIZATION clinic_owner",
        "SET TIME ZONE 'UTC'",
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
        "SET",
    ],
)
def test_unresolved_set_forms_are_opaque(statement: str) -> None:
    assert "unresolved SET setting" in references(statement).opaque


@PARSER_ONLY
def test_update_set_clause_is_not_a_setting() -> None:
    parsed = references("UPDATE t SET a = 1")
    assert not parsed.settings
    assert not parsed.opaque


def test_function_body_set_statement_is_in_the_closure() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION pg_temp.t6_set_writer() RETURNS void LANGUAGE plpgsql"
            " AS $f$ BEGIN SET LOCAL app.current_user_id = ''; END $f$"
        )
        reads = Catalog().statement("SELECT pg_temp.t6_set_writer()")
        transaction.set_rollback(True)
    assert "app.current_user_id" in reads.settings


def test_default_argument_setting_read_is_in_the_closure(rbac_graph: RbacGraph) -> None:
    # A parameter DEFAULT is evaluated in the caller, outside the body text.
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION pg_temp.t6_default_reader("
            "actor text DEFAULT current_setting('app.current_user_id', true))"
            " RETURNS text LANGUAGE sql STABLE AS $f$ SELECT actor $f$"
        )
    observer = _observe(rbac_graph, "SELECT pg_temp.t6_default_reader()")
    assert ("guc", "app.current_user_id") in observer.touches


@pytest.mark.parametrize("relation", ["pg_settings", "pg_catalog.pg_settings"])
def test_setting_enumeration_is_opaque(rbac_graph: RbacGraph, relation: str) -> None:
    observer = _observe(
        rbac_graph,
        f"SELECT setting FROM {relation} WHERE name = 'app.current_user_id'",  # noqa: S608 - fixed test identifiers.
    )
    assert ("opaque", "setting enumeration pg_settings") in observer.touches


def test_dynamic_sql_is_not_certified_even_when_it_returns_no_authority_rows(
    rbac_graph: RbacGraph,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION pg_temp.t6_dynamic() RETURNS boolean LANGUAGE plpgsql
            AS $body$ DECLARE answer boolean; BEGIN
                EXECUTE 'SELECT true' INTO answer;
                RETURN answer;
            END $body$
        """)
        cursor.execute("GRANT EXECUTE ON FUNCTION pg_temp.t6_dynamic() TO clinic_app")
    observer = _observe(rbac_graph, "SELECT pg_temp.t6_dynamic()")
    assert ("opaque", "execute") in observer.touches


def test_sql_standard_body_exposes_actor_setting_reads(rbac_graph: RbacGraph) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION pg_temp.t6_standard() RETURNS text LANGUAGE sql
            RETURN current_setting('app.current_user_id', true)
        """)
        cursor.execute("GRANT EXECUTE ON FUNCTION pg_temp.t6_standard() TO clinic_app")
    observer = _observe(rbac_graph, "SELECT pg_temp.t6_standard()")
    assert ("guc", "app.current_user_id") in observer.touches


def test_operator_over_a_setting_reader_is_not_an_escape(rbac_graph: RbacGraph) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION clinic_app.t6_operator(integer, integer)
            RETURNS boolean LANGUAGE sql AS $body$
                SELECT current_setting('app.current_user_id', true) IS NOT NULL
            $body$;
            CREATE OPERATOR clinic_app.?#? (
                LEFTARG=integer, RIGHTARG=integer, FUNCTION=clinic_app.t6_operator
            );
            GRANT EXECUTE ON FUNCTION clinic_app.t6_operator(integer,integer)
            TO clinic_app;
        """)
    try:
        observer = _observe(rbac_graph, "SELECT 1 OPERATOR(clinic_app.?#?) 2")
        assert ("guc", "app.current_user_id") in observer.touches
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP OPERATOR clinic_app.?#? (integer,integer)")
            cursor.execute("DROP FUNCTION clinic_app.t6_operator(integer,integer)")


def test_native_alias_cannot_inherit_a_builtin_allowance(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
) -> None:
    # CREATE LANGUAGE internal is privileged; use the existing test admin URL.
    with psycopg.connect(superuser_database_url, autocommit=True) as admin:
        admin.execute("""
            CREATE FUNCTION clinic_app.t6_native(text,boolean) RETURNS text
            LANGUAGE internal STABLE AS 'show_config_by_name_missing_ok';
            GRANT EXECUTE ON FUNCTION clinic_app.t6_native(text,boolean) TO clinic_app;
        """)
        try:
            observer = _observe(
                rbac_graph,
                "SELECT clinic_app.t6_native('app.current_user_id',true)",
            )
            assert (
                "opaque",
                "opaque function clinic_app.t6_native",
            ) in observer.touches
        finally:
            admin.execute("DROP FUNCTION clinic_app.t6_native(text,boolean)")


def test_domain_cast_with_hidden_actor_read_is_uninspectable(
    rbac_graph: RbacGraph,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE DOMAIN pg_temp.t6_domain AS text
            CHECK (current_setting('app.current_user_id',true) IS NOT NULL)
        """)
    observer = _observe(rbac_graph, "SELECT 'value'::pg_temp.t6_domain")
    assert any(kind == "opaque" for kind, _name in observer.touches)


@pytest.mark.parametrize(
    "argument",
    ["'app.current_' || 'user_id'", "'app.current_'\n'user_id'"],
)
def test_computed_setting_argument_is_not_a_literal_exemption(
    rbac_graph: RbacGraph, argument: str
) -> None:
    observer = _observe(rbac_graph, f"SELECT current_setting({argument}, true)")
    assert ("opaque", "computed setting name") in observer.touches


@pytest.mark.parametrize("expression", ["default", "check", "generated"])
def test_implicit_relation_expression_cannot_hide_actor_reads(
    rbac_graph: RbacGraph, expression: str
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION pg_temp.t6_implicit_actor() RETURNS text
            LANGUAGE sql IMMUTABLE AS $body$
                SELECT current_setting('app.current_user_id', true)
            $body$
        """)
        clause = {
            "default": "DEFAULT pg_temp.t6_implicit_actor()",
            "check": "CHECK (value <> pg_temp.t6_implicit_actor())",
            "generated": "GENERATED ALWAYS AS (pg_temp.t6_implicit_actor()) STORED",
        }[expression]
        cursor.execute(f"CREATE TEMP TABLE t6_implicit (value text {clause})")
        cursor.execute("GRANT ALL ON t6_implicit TO clinic_app")
    observer = _observe(
        rbac_graph, "INSERT INTO pg_temp.t6_implicit DEFAULT VALUES RETURNING value"
    )
    assert ("guc", "app.current_user_id") in observer.touches


def test_raw_libpq_cannot_bypass_observation(rbac_graph: RbacGraph) -> None:
    actor = User.objects.create(username="synthetic-raw-observer-" + uuid4().hex)
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    probe = DifferentialProbe(__name__ + "._raw_query", _raw_query)
    assert _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
    with pytest.raises(AuthorityObservedError):
        observer.assert_nonstaff(probe.symbol)


def _second_connection() -> bool:
    with psycopg.connect(os.environ["APP_DATABASE_URL"]) as raw:
        assert raw.execute("SELECT 1").fetchone() == (1,)
    return True


def test_unobserved_backend_is_not_an_exemption(rbac_graph: RbacGraph) -> None:
    actor = User.objects.create(username="synthetic-connection-observer-" + uuid4().hex)
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    probe = DifferentialProbe(__name__ + "._second_connection", _second_connection)
    assert _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
    with pytest.raises(AuthorityObservedError):
        observer.assert_nonstaff(probe.symbol)
    assert ("opaque", "SQL on an unobserved connection") in observer.touches
