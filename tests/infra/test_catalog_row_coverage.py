"""Exercise the server's catalog-column universe without writing system catalogs.

Each CTE makes a PostgreSQL-typed scratch row, alters one field, and sends
both copies through the production whole-row projector and resume decision.
DDL-only deparsers are exercised on real DDL below, not mocked node strings.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from apps.tenancy.migrations._resume_catalog import (
    Checkpoint,
    ResumeCatalogError,
    match_boundary,
    snapshot,
)
from apps.tenancy.migrations._resume_catalog_sql import (
    CTES,
    SOURCES,
    VOLATILE,
    Field,
    fields,
    row_definition,
)
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from apps.tenancy.migrations._resume_catalog import Catalog
    from django.db.backends.postgresql.base import DatabaseWrapper
    from pytest_django.plugin import DjangoDbBlocker


DDL = """
CREATE TABLE clinic_app.catalog_probe (id integer GENERATED ALWAYS AS IDENTITY,
    n integer DEFAULT 2 CONSTRAINT catalog_n_positive CHECK (n > 0),
    g integer GENERATED ALWAYS AS (n + 1) STORED);
INSERT INTO clinic_app.catalog_probe(n) VALUES (3);
ALTER TABLE clinic_app.catalog_probe ADD COLUMN fast integer DEFAULT 4;
ALTER TABLE clinic_app.catalog_probe ALTER fast DROP DEFAULT;
CREATE TABLE clinic_app.catalog_probe_peer
    (LIKE clinic_app.catalog_probe INCLUDING ALL);
CREATE TABLE clinic_app.catalog_parts (n integer) PARTITION BY RANGE (n);
CREATE TABLE clinic_app.catalog_part PARTITION OF clinic_app.catalog_parts
    FOR VALUES FROM (0) TO (10);
CREATE INDEX catalog_probe_idx ON clinic_app.catalog_probe ((n + 3)) WHERE n > 2;
CREATE POLICY catalog_policy ON clinic_app.catalog_probe
    AS PERMISSIVE USING (n > 1) WITH CHECK (n > 2);
CREATE FUNCTION clinic_app.catalog_fn(n integer DEFAULT 2)
    RETURNS integer LANGUAGE SQL VOLATILE SECURITY DEFINER RETURN n + 1;
CREATE FUNCTION clinic_app.catalog_tg() RETURNS trigger LANGUAGE plpgsql
    AS $$ BEGIN RETURN NEW; END $$;
CREATE TRIGGER catalog_cond BEFORE UPDATE ON clinic_app.catalog_probe
    FOR EACH ROW WHEN (OLD.n < NEW.n) EXECUTE FUNCTION clinic_app.catalog_tg();
CREATE VIEW clinic_app.catalog_view AS SELECT n FROM clinic_app.catalog_probe;
CREATE RULE meta_rule AS ON UPDATE TO clinic_app.catalog_probe
    WHERE OLD.n > 1 DO NOTHING;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA clinic_app;
GRANT SELECT(n) ON clinic_app.catalog_probe TO clinic_app;
"""

# These two trees are deparsed via the stored rule's identity, not by feeding
# independently forgeable pg_node_tree text to PostgreSQL's internal reader.
DDL_FIELDS = {
    ("pg_rewrite", "ev_action"),
    ("pg_rewrite", "ev_qual"),
    ("pg_proc", "prosqlbody"),
    ("pg_proc", "proargdefaults"),
    ("pg_attrdef", "adbin"),
    ("pg_index", "indexprs"),
    ("pg_index", "indpred"),
    ("pg_constraint", "conbin"),
    ("pg_trigger", "tgqual"),
    ("pg_policy", "polqual"),
    ("pg_policy", "polwithcheck"),
    ("pg_class", "relpartbound"),
}


def _sample(field: Field) -> sql.Composable:
    """Prefer real, non-null values, including expression trees and anyarray."""
    catalog, name, kind, target, _ = field
    guards = [sql.SQL("true")]
    if catalog == "pg_proc":
        guards.append(sql.SQL("s.prokind IN ('f','p')"))
    if catalog == "pg_trigger":
        guards.append(sql.SQL("NOT s.tgisinternal"))
    column = sql.Identifier("s", name)
    # A database with no FDW column options, extension config, parent trigger,
    # etc. still has typed copies of these rows. The alternate is a typed value.
    order = sql.SQL("{} IS NOT NULL DESC").format(column)
    if target and kind in {"oid", "regproc"}:
        order = sql.SQL("{}::oid <> 0 DESC NULLS LAST").format(column)
    return sql.SQL(
        "SELECT s.* FROM (SELECT r.* FROM pg_catalog.{} r {}) s "
        "WHERE {} ORDER BY {} LIMIT 1"
    ).format(
        sql.Identifier(catalog),
        sql.SQL(SOURCES[catalog].joins),
        sql.SQL(" AND ").join(guards),
        order,
    )


def _donor(field: Field) -> sql.Composable:
    _, name, kind, target, vector = field
    if target:
        # Use an existing logical FK, never a random local OID. For a numeric
        # vector preserve the PostgreSQL type (and thus its catalog semantics).
        col = sql.Identifier("seed", name)
        previous = (
            sql.SQL("(SELECT x FROM unnest({}) x LIMIT 1)").format(col)
            if vector
            else col
        )
        if target == "pg_class" and name in {
            "polrelid",
            "adrelid",
            "conrelid",
            "indrelid",
        }:
            # Trees on this row may refer to n at attnum 2. Use a compatible
            # real row type rather than an arbitrary one-column system index.
            value = sql.SQL(
                "(CASE WHEN {}='catalog_probe_peer'::regclass THEN 'catalog_probe' "
                "ELSE 'catalog_probe_peer' END)::regclass::oid"
            ).format(previous)
        else:
            value = sql.SQL(
                "(SELECT oid FROM pg_catalog.{} WHERE oid IS DISTINCT FROM {}::oid "
                "ORDER BY oid LIMIT 1)"
            ).format(
                sql.Identifier("pg_roles" if target == "pg_authid" else target),
                previous,
            )
        if vector:
            return sql.SQL("ARRAY[{}]::{}").format(value, sql.SQL(kind))
        return sql.SQL("{}::{}").format(value, sql.SQL(kind))
    values = {
        "text[]": "ARRAY['probe']::text[]",
        "smallint[]": "ARRAY[1::int2]",
        "int2vector": "ARRAY[1::int2]::int2vector",
        "aclitem[]": "ARRAY['=r/clinic_owner'::aclitem]",
        "bytea": "'a'::bytea",
        '"char"[]': "ARRAY['i'::\"char\"]",
    }
    return sql.SQL(values[kind])


def _variant(field: Field) -> sql.Composable:
    _, name, kind, target, _ = field
    column = sql.Identifier("seed", name)
    if kind == "boolean":
        return sql.SQL("NOT {}").format(column)
    if not target and kind in {"smallint", "integer", "bigint", "real"}:
        return sql.SQL("CASE WHEN {}=1 THEN 2 ELSE 1 END::{}").format(
            column, sql.SQL(kind)
        )
    if not target and kind in {"text", "name", '"char"'}:
        return sql.SQL("(CASE WHEN {}::text='x' THEN 'y' ELSE 'x' END)::{}").format(
            column, sql.SQL(kind)
        )
    if kind == "anyarray":
        # Null is a valid typed alternate; never manufacture binary parse trees.
        return sql.SQL("NULL::{}").format(sql.SQL(kind))
    if target:
        return _donor(field)
    # int2vector has a lower bound of zero; JSON preserves ordered elements,
    # not a separate SQL-array lower bound. Mutate the logical contents.
    return sql.SQL(
        "CASE WHEN to_jsonb({c}) IS DISTINCT FROM to_jsonb({d}) THEN {d} ELSE NULL END"
    ).format(c=column, d=_donor(field))


def _copies(
    db: DatabaseWrapper, schema: tuple[Field, ...], field: Field
) -> tuple[dict[str, str], dict[str, str]]:
    catalog, name, *_ = field
    samples = [f.name for f in schema if f.catalog == catalog]
    payload = sql.SQL(row_definition(catalog, schema))
    # xmin is a system column, not part of a stored composite value. Give both
    # scratch projections the same version for the OTP-only provenance join.
    extra = sql.SQL(", '0'::xid AS xmin") if catalog == "pg_attribute" else sql.SQL("")
    projections = []
    for changed in (False, True):
        columns = sql.SQL(",").join(
            sql.SQL("{} AS {}").format(
                _variant(field)
                if changed and col == name
                else sql.Identifier("seed", col),
                sql.Identifier(col),
            )
            for col in samples
        )
        projections.append(
            sql.SQL(
                "SELECT ({})::text FROM (SELECT {} {} FROM seed) r "
                "CROSS JOIN (VALUES ('relation catalog_probe')) m(parent) "
                "CROSS JOIN LATERAL (SELECT {} AS logical_name) t"
            ).format(
                payload,
                columns,
                extra,
                sql.SQL("r.tgname::text") if catalog == "pg_trigger" else sql.SQL("''"),
            )
        )
    with db.cursor() as cursor:
        cursor.execute(
            sql.SQL(CTES)
            + sql.SQL(", seed AS MATERIALIZED ({}) ").format(_sample(field))
            + sql.SQL(" UNION ALL ").join(projections)
        )
        original, mutant = (json.loads(row[0]) for row in cursor.fetchall())
    assert name in original, (catalog, name)
    assert name in mutant, (catalog, name)
    assert original[name] != mutant[name], (catalog, name)
    return (
        {k: json.dumps(v, sort_keys=True) for k, v in original.items()},
        {k: json.dumps(v, sort_keys=True) for k, v in mutant.items()},
    )


def _refuses(name: str, original: dict[str, str], mutant: dict[str, str]) -> None:
    checkpoint = Checkpoint("catalog-meta", frozenset({name}), 1)
    states: list[Catalog] = [{name: {}}, {name: original}]
    assert match_boundary(checkpoint, {name: original}, states)
    with pytest.raises(ResumeCatalogError, match="conflicting catalog"):
        match_boundary(checkpoint, {name: mutant}, states)


def test_every_catalog_column_participates_in_resume(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        MigrationExecutor(db).recorder.ensure_schema()
        with db.cursor() as cursor:
            cursor.execute(DDL)
        schema = fields(db)
        seen = set()
        for field in schema:
            key = (field.catalog, field.name)
            if field.name in VOLATILE.get(field.catalog, ()) or key in DDL_FIELDS:
                continue
            before, after = _copies(db, schema, field)
            _refuses(".".join(key), before, after)
            seen.add(key)
        assert seen | DDL_FIELDS == {
            (f.catalog, f.name)
            for f in schema
            if f.name not in VOLATILE.get(f.catalog, ())
        }
        assert {c for c, _ in seen} == SOURCES.keys()
        # A server upgrade adding an ordinary catalog column must not require
        # a new allowlist entry. Exhibit an unknown field on a typed copy.
        with db.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT ({})::text FROM (SELECT s.*, v.future_attribute "
                    "FROM pg_sequence s CROSS JOIN (VALUES (1), (2)) "
                    "v(future_attribute) WHERE seqrelid="
                    "pg_get_serial_sequence('catalog_probe','id')::regclass) r "
                    "ORDER BY r.future_attribute"
                ).format(sql.SQL(row_definition("pg_sequence", schema)))
            )
            first, second = (json.loads(row[0]) for row in cursor.fetchall())
        assert (first["future_attribute"], second["future_attribute"]) == (1, 2)
        _refuses(
            "pg_sequence",
            {k: json.dumps(v) for k, v in first.items()},
            {k: json.dumps(v) for k, v in second.items()},
        )


def test_catalog_ddl_semantics_refuse_drift(django_db_blocker: DjangoDbBlocker) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        MigrationExecutor(db).recorder.ensure_schema()
        with db.cursor() as cursor:
            cursor.execute(DDL)
        table = "relation catalog_probe"
        function = "function catalog_fn(integer)"
        for name, attribute, ddl in [
            (
                table,
                "columns.n.default.adbin",
                "ALTER TABLE catalog_probe ALTER n SET DEFAULT 17",
            ),
            (
                table,
                "constraints.catalog_n_positive.conbin",
                "ALTER TABLE catalog_probe DROP CONSTRAINT catalog_n_positive, "
                "ADD CONSTRAINT catalog_n_positive CHECK (n >= 0)",
            ),
            (
                table,
                "indexes.catalog_probe_idx.pg_index.indexprs",
                "DROP INDEX catalog_probe_idx; CREATE INDEX catalog_probe_idx "
                "ON catalog_probe ((n + 4)) WHERE n > 2",
            ),
            (
                table,
                "indexes.catalog_probe_idx.pg_index.indpred",
                "DROP INDEX catalog_probe_idx; CREATE INDEX catalog_probe_idx "
                "ON catalog_probe ((n + 4)) WHERE n >= 2",
            ),
            (
                table,
                "triggers.catalog_cond.tgqual",
                "CREATE OR REPLACE TRIGGER catalog_cond BEFORE UPDATE ON catalog_probe "
                "FOR EACH ROW WHEN (OLD.n <= NEW.n) EXECUTE FUNCTION catalog_tg()",
            ),
            (
                table,
                "policies.catalog_policy.polqual",
                "ALTER POLICY catalog_policy ON catalog_probe USING (n >= 1)",
            ),
            (
                table,
                "policies.catalog_policy.polwithcheck",
                "ALTER POLICY catalog_policy ON catalog_probe WITH CHECK (n >= 2)",
            ),
            (
                table,
                "columns.g.default.adbin",
                "ALTER TABLE catalog_probe DROP COLUMN g;"
                "ALTER TABLE catalog_probe ADD g integer "
                "GENERATED ALWAYS AS (n + 2) STORED",
            ),
            (function, "provolatile", "ALTER FUNCTION catalog_fn(integer) STABLE"),
            (
                function,
                "proargdefaults",
                "CREATE OR REPLACE FUNCTION catalog_fn(n integer DEFAULT 3) "
                "RETURNS integer LANGUAGE SQL STABLE SECURITY DEFINER RETURN n + 1",
            ),
            (
                function,
                "prosecdef",
                "ALTER FUNCTION catalog_fn(integer) SECURITY INVOKER",
            ),
            (
                function,
                "prosqlbody",
                "CREATE OR REPLACE FUNCTION catalog_fn(n integer DEFAULT 3) "
                "RETURNS integer LANGUAGE SQL STABLE RETURN n + 3",
            ),
            (
                table,
                "policies.catalog_policy.polpermissive",
                "DROP POLICY catalog_policy ON catalog_probe;"
                "CREATE POLICY catalog_policy ON catalog_probe AS RESTRICTIVE "
                "USING (n > 1) WITH CHECK (n > 2)",
            ),
            (
                "relation catalog_view",
                "rules._RETURN.ev_action",
                "CREATE OR REPLACE VIEW catalog_view AS "
                "SELECT n + 1 AS n FROM catalog_probe",
            ),
            (
                table,
                "rules.meta_rule.ev_qual",
                "CREATE OR REPLACE RULE meta_rule AS ON UPDATE TO catalog_probe "
                "WHERE OLD.n > 3 DO NOTHING",
            ),
            (
                "relation catalog_part",
                "relpartbound",
                "ALTER TABLE catalog_parts DETACH PARTITION catalog_part; "
                "ALTER TABLE catalog_parts ATTACH PARTITION catalog_part "
                "FOR VALUES FROM (-1) TO (10)",
            ),
        ]:
            before = snapshot(db)[name]
            with db.cursor() as cursor:
                cursor.execute(ddl)
            after = snapshot(db)[name]
            assert before.get(attribute) != after[attribute]
            _refuses(name, before, after)
