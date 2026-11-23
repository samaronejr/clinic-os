"""Fingerprint PostgreSQL catalog *rows*, not a projection of known attributes.

The closed list below is the entire exclusion policy. ``to_jsonb(row)`` retains
every other user column; PostgreSQL's implicit tuple header (xmin/xmax, ctid,
etc.) is not part of a composite value. OID foreign keys are discovered from
pg_get_catalog_foreign_keys and resolved by the server. Expressions and ACLs
are normalized **in place**; their columns are not excluded.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, NamedTuple

from psycopg import sql

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper

# oid is the local surrogate of the row whose logical identity is in its path.
# relfilenode/relrewrite are physical rewrite locations; relfrozenxid/minmxid
# advance with VACUUM. relpages/reltuples/relallvisible are planner estimates.
# The four relhas* bits are lazily cleared hints; the owned rows, not a VACUUM
# timing decision, define index/trigger/rule/inheritance membership.
VOLATILE: dict[str, tuple[str, ...]] = {
    "pg_class": (
        "oid",
        "relfilenode",
        "relrewrite",
        "relfrozenxid",
        "relminmxid",
        "relpages",
        "reltuples",
        "relallvisible",
        "relhasindex",
        "relhastriggers",
        "relhasrules",
        "relhassubclass",
    ),
    "pg_attrdef": ("oid",),
    "pg_constraint": ("oid",),
    "pg_policy": ("oid",),
    "pg_trigger": ("oid",),
    "pg_proc": ("oid",),
    "pg_extension": ("oid",),
    "pg_rewrite": ("oid",),
}


class RowSource(NamedTuple):
    """Parent object, path, and the join selecting its actual catalog rows."""

    parent: str
    path: str
    joins: str


_OWNER = "'relation ' || owner.relname"
_REL = "JOIN owners AS owner ON owner.oid = r."
_FUN = (
    "'function ' || r.proname || '(' || pg_catalog.oidvectortypes(r.proargtypes) || ')'"
)
_MEMBER = "JOIN members AS m ON m.oid = r."
SOURCES = {
    "pg_class": RowSource("m.parent", "m.path", _MEMBER + "oid"),
    "pg_attribute": RowSource(
        "m.parent",
        "m.path || ARRAY['columns', r.attname::text]",
        _MEMBER + "attrelid WHERE r.attnum > 0 ORDER BY r.attrelid, r.attnum",
    ),
    "pg_attrdef": RowSource(
        _OWNER,
        "ARRAY['columns', a.attname::text, 'default']",
        _REL + "adrelid JOIN pg_catalog.pg_attribute a "
        "ON a.attrelid=r.adrelid AND a.attnum=r.adnum",
    ),
    "pg_constraint": RowSource(
        _OWNER, "ARRAY['constraints', r.conname]", _REL + "conrelid"
    ),
    "pg_index": RowSource(
        _OWNER,
        "ARRAY['indexes', i.relname::text, 'pg_index']",
        _REL + "indrelid JOIN pg_catalog.pg_class i ON i.oid=r.indexrelid",
    ),
    "pg_policy": RowSource(_OWNER, "ARRAY['policies', r.polname]", _REL + "polrelid"),
    "pg_trigger": RowSource(
        _OWNER,
        "ARRAY['triggers', t.logical_name]",
        _REL + "tgrelid JOIN trigger_names t ON t.oid=r.oid",
    ),
    "pg_proc": RowSource(
        _FUN,
        "ARRAY[]::text[]",
        "WHERE r.pronamespace='clinic_app'::regnamespace AND r.prokind IN ('f','p')",
    ),
    "pg_sequence": RowSource(_OWNER, "ARRAY['sequence']", _REL + "seqrelid"),
    "pg_extension": RowSource(
        "'extension ' || r.extname",
        "ARRAY[]::text[]",
        "WHERE r.extname='unaccent'",
    ),
    "pg_rewrite": RowSource(_OWNER, "ARRAY['rules', r.rulename]", _REL + "ev_class"),
    "pg_inherits": RowSource(
        _OWNER,
        "ARRAY['inherits', r.inhparent::regclass::text]",
        _REL + "inhrelid",
    ),
}

CTES = """
WITH owners AS MATERIALIZED (
    SELECT oid, relname FROM pg_catalog.pg_class
    WHERE relnamespace='clinic_app'::regnamespace AND relkind NOT IN ('i','I')
), members AS MATERIALIZED (
    SELECT oid, 'relation ' || relname AS parent, ARRAY[]::text[] AS path FROM owners
    UNION ALL
    SELECT i.indexrelid, 'relation ' || o.relname,
           ARRAY['indexes', c.relname::text, 'pg_class']
    FROM owners o JOIN pg_catalog.pg_index i ON i.indrelid=o.oid
    JOIN pg_catalog.pg_class c ON c.oid=i.indexrelid
), trigger_names AS MATERIALIZED (
    SELECT t.oid, CASE WHEN t.tgisinternal AND c.contype='f'
            AND t.tgname ~ '^RI_ConstraintTrigger_[ac]_[0-9]+$'
        THEN c.conrelid::regclass::text || ':' || c.conname || ':' ||
             t.tgrelid::regclass::text || ':' || t.tgfoid::regprocedure::text
        ELSE t.tgname::text END AS logical_name
    FROM pg_catalog.pg_trigger t LEFT JOIN pg_catalog.pg_constraint c
        ON c.oid=t.tgconstraint
)
"""

# Expressions replace server-private parse trees with the server's deparser.
# WHEN can refer to both OLD and NEW; pg_get_expr cannot deparse that form.
# The trigger deparser may prepend CONSTRAINT even for an internal trigger.
_TRIGGER_DEF = (
    "replace(pg_catalog.pg_get_triggerdef(r.oid), "
    "'TRIGGER ' || pg_catalog.quote_ident(r.tgname) || ' ', "
    "'TRIGGER ' || pg_catalog.quote_ident(t.logical_name) || ' ')"
)
_SPECIAL = {
    # The third-party OTP AddField(auto_now_add=True) writes the install-time
    # clock as a fast default. Only the *original* atomic DDL tuple, witnessed
    # by its recorder transaction, can stand for this symbolic clock. Never
    # adopt an unverified value from the current database as a new reference.
    ("pg_attribute", "attmissingval"): """
        CASE WHEN r.atthasmissing AND r.attname='created_at' AND EXISTS (
            SELECT FROM clinic_app.django_migrations dm
            WHERE dm.xmin=r.xmin AND dm.name='0003_add_timestamps'
              AND (dm.app,m.parent) IN (
                ('otp_totp','relation otp_totp_totpdevice'),
                ('otp_static','relation otp_static_staticdevice')
              )
              AND (dm.app='otp_totp')=(m.parent='relation otp_totp_totpdevice')
        ) THEN to_jsonb('original atomic OTP migration clock'::text)
        ELSE to_jsonb(r.attmissingval) END
    """,
    ("pg_class", "relpartbound"): "pg_catalog.pg_get_expr(r.relpartbound,r.oid)",
    ("pg_class", "reltoastrelid"): (
        "(SELECT COALESCE('TOAST of ' || "
        "substring(c.relname from '^pg_toast_([0-9]+)$')"
        "::oid::regclass::text, c.oid::regclass::text) "
        "FROM pg_catalog.pg_class c WHERE c.oid=r.reltoastrelid)"
    ),
    ("pg_attrdef", "adbin"): "pg_catalog.pg_get_expr(r.adbin,r.adrelid)",
    ("pg_constraint", "conbin"): "pg_catalog.pg_get_expr(r.conbin,r.conrelid)",
    ("pg_index", "indexprs"): "pg_catalog.pg_get_expr(r.indexprs,r.indrelid)",
    ("pg_index", "indpred"): "pg_catalog.pg_get_expr(r.indpred,r.indrelid)",
    ("pg_policy", "polqual"): "pg_catalog.pg_get_expr(r.polqual,r.polrelid)",
    ("pg_policy", "polwithcheck"): "pg_catalog.pg_get_expr(r.polwithcheck,r.polrelid)",
    ("pg_trigger", "tgname"): "t.logical_name",
    ("pg_trigger", "tgqual"): f"CASE WHEN r.tgqual IS NOT NULL THEN {_TRIGGER_DEF} END",
    ("pg_proc", "proargdefaults"): "pg_catalog.pg_get_expr(r.proargdefaults,0)",
    ("pg_proc", "prosqlbody"): "pg_catalog.pg_get_function_sqlbody(r.oid)",
    ("pg_rewrite", "ev_action"): "pg_catalog.pg_get_ruledef(r.oid)",
    ("pg_rewrite", "ev_qual"): "pg_catalog.pg_get_ruledef(r.oid)",
}
_DEFINITION = {
    "pg_constraint": "pg_catalog.pg_get_constraintdef(r.oid)",
    "pg_index": "pg_catalog.pg_get_indexdef(r.indexrelid)",
    "pg_trigger": _TRIGGER_DEF,
    "pg_proc": "pg_catalog.pg_get_functiondef(r.oid)",
}

# Common reg* casts are much cheaper than pg_identify_object for every column
# of every relation. Other catalog FK targets use the generic object address.
_REG = {
    "pg_class": "regclass",
    "pg_type": "regtype",
    "pg_namespace": "regnamespace",
    "pg_proc": "regprocedure",
    "pg_operator": "regoperator",
    "pg_authid": "regrole",
    "pg_collation": "regcollation",
}


def _identity(target: str, value: str) -> str:
    if target in _REG:
        expr = f"{value}::oid::{_REG[target]}::text"
    elif target == "pg_trigger":
        expr = (
            sql.SQL("(SELECT logical_name FROM trigger_names WHERE oid={})")
            .format(sql.SQL(value))
            .as_string()
        )
    else:
        expr = (
            sql.SQL("(pg_catalog.pg_identify_object({}::regclass,{},0)).identity")
            .format(sql.Literal("pg_catalog." + target), sql.SQL(value))
            .as_string()
        )
    return f"CASE WHEN {value}::oid=0 THEN '(none)' ELSE {expr} END"


class Field(NamedTuple):
    catalog: str
    name: str
    type_name: str
    target: str | None
    vector: bool | None


_FIELDS_SQL = """
SELECT c.relname, a.attname, a.atttypid::regtype::text, f.target, f.is_array
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
LEFT JOIN (
    SELECT fktable, fkcols[1] AS name, target.relname AS target, is_array
    FROM pg_catalog.pg_get_catalog_foreign_keys()
    JOIN pg_catalog.pg_class target ON target.oid=pktable
    WHERE pkcols=ARRAY['oid']::text[] AND cardinality(fkcols)=1
) f ON f.fktable=c.oid AND f.name=a.attname
WHERE c.relnamespace='pg_catalog'::regnamespace AND c.relname=ANY(%s)
  AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum
"""


def fields(connection: BaseDatabaseWrapper) -> tuple[Field, ...]:
    """Use the running server's schema; new scalar or FK fields are not opt-in."""
    with connection.cursor() as cursor:
        cursor.execute(_FIELDS_SQL, [list(SOURCES)])
        return tuple(Field(*row) for row in cursor.fetchall())


def _normalizer(field: Field) -> str | None:
    catalog, name, kind, target, vector = field
    value = 'r."' + name.replace('"', '""') + '"'
    if expr := _SPECIAL.get((catalog, name)):
        return expr
    if kind == "aclitem[]":
        return (
            sql.SQL(
                "CASE WHEN {v} IS NOT NULL THEN COALESCE((SELECT jsonb_object_agg("
                "CASE WHEN grantee=0 THEN 'PUBLIC' ELSE grantee::regrole::text END "
                "|| ':' || privilege_type || ':' || grantor::regrole::text, "
                "is_grantable) "
                "FROM pg_catalog.aclexplode({v})), '{{}}'::jsonb) END"
            )
            .format(v=sql.SQL(value))
            .as_string()
        )
    if target and vector:
        identity = _identity(target, "element")
        order = "1" if (catalog, name) == ("pg_policy", "polroles") else "ordinal"
        return (
            sql.SQL(
                "CASE WHEN {v} IS NOT NULL THEN ARRAY(SELECT {identity} "
                "FROM pg_catalog.unnest({v}) WITH ORDINALITY AS u(element,ordinal) "
                "ORDER BY {order}) END"
            )
            .format(v=sql.SQL(value), identity=sql.SQL(identity), order=sql.SQL(order))
            .as_string()
        )
    if target:
        return _identity(target, value)
    if kind in {"oid", "oidvector", "oid[]", "pg_node_tree"}:
        msg = f"Unresolved catalog reference or expression: {catalog}.{name}"
        raise ValueError(msg)
    return None


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def row_definition(catalog: str, schema: tuple[Field, ...]) -> str:
    """Whole-row JSONB minus only volatile fields, overlaid with logical values."""
    excluded = VOLATILE.get(catalog, ())
    payload = "pg_catalog.to_jsonb(r)"
    if excluded:
        payload += " - ARRAY[" + ",".join(map(_literal, excluded)) + "]"
    replacements = [
        part
        for field in schema
        if field.catalog == catalog and field.name not in excluded
        if (expr := _normalizer(field)) is not None
        for part in (_literal(field.name), expr)
    ]
    if catalog in _DEFINITION:
        replacements += ["'definition'", _DEFINITION[catalog]]
    if replacements:
        payload += " || pg_catalog.jsonb_build_object(" + ",".join(replacements) + ")"
    return payload


@cache
def catalog_sql(schema: tuple[Field, ...]) -> str:
    """Build a single consistent MVCC read across all owned catalog rows."""
    return CTES + "\nUNION ALL\n".join(
        sql.SQL("(SELECT {}, {}, ({})::text FROM pg_catalog.{} r {})")
        .format(
            sql.SQL(s.parent),
            sql.SQL(s.path),
            sql.SQL(row_definition(catalog, schema)),
            sql.Identifier(catalog),
            sql.SQL(s.joins),
        )
        .as_string()
        for catalog, s in SOURCES.items()
    )
