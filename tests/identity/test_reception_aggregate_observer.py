"""Native JSON aggregation is data-only; application aliases never inherit trust."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from django.db import connection

from identity.authority_catalog import Catalog
from identity.authority_observer import AuthorityObservedError, AuthorityObserver
from identity.nonstaff_differential import DifferentialProbe, _invoke

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _query(statement: str) -> object:
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()


@pytest.mark.parametrize("mode", ["builtin", "application", "search_path"])
def test_jsonb_aggregate_requires_exact_native_identity(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    mode: str,
    record_property: Callable[[str, object], None],
) -> None:
    actor = User.objects.create(username="synthetic-json-aggregate-" + uuid4().hex)
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        owner.execute(
            "CREATE FUNCTION clinic_app.jsonb_agg(jsonb) RETURNS jsonb "
            "LANGUAGE internal IMMUTABLE AS 'jsonb_strip_nulls'"
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW search_path")
                previous = cursor.fetchone()
                assert previous is not None
                if mode == "search_path":
                    cursor.execute("SET search_path=clinic_app,pg_catalog,public")
                cursor.execute(
                    "SELECT p.oid, n.nspname||'.'||p.proname||'('||"
                    "oidvectortypes(p.proargtypes)||')' FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE p.oid='pg_catalog.jsonb_agg(anyelement)'::regprocedure"
                )
                native = cursor.fetchone()
                assert native is not None
                record_property(
                    "jsonb_aggregate_native_identity",
                    json.dumps({"oid": native[0], "signature": native[1]}),
                )
            try:
                catalog = Catalog()
                observer = AuthorityObserver(catalog, catalog.derive())
                statement = (
                    "SELECT pg_catalog.jsonb_agg(1)"
                    if mode == "builtin"
                    else "SELECT "
                    + ("clinic_app." if mode == "application" else "")
                    + 'jsonb_agg(\'{"safe":1,"omit":null}\'::jsonb)'
                )
                probe = DifferentialProbe(
                    __name__ + "._query", lambda: _query(statement)
                )
                assert _invoke(
                    probe, actor, rbac_graph.organization_a, authority=observer
                )
                if mode == "builtin":
                    observer.assert_nonstaff(probe.symbol)
                else:
                    with pytest.raises(AuthorityObservedError):
                        observer.assert_nonstaff(probe.symbol)
                    assert (
                        "opaque",
                        "opaque function clinic_app.jsonb_agg",
                    ) in observer.touches
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('search_path',%s,false)", [previous[0]]
                    )
        finally:
            owner.execute("DROP FUNCTION clinic_app.jsonb_agg(jsonb)")
