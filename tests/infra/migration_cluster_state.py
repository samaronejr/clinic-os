"""Compare complete logical row bytes on the private PostgreSQL recovery clone."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

import psycopg
from psycopg import sql

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.backends.utils import CursorWrapper

# The automatic maintenance worker can change only this bookkeeping between
# the two reads. pg_stat_* / pg_statio_* are computed views, not stored rows;
# their definitions, including ACLs, still occur in the catalogs below. Do not
# wildcard pg_statistic_ext: its extended-statistics *definitions* are schema.
_STATISTICS = {"pg_statistic", "pg_statistic_ext_data"}
_ESTIMATES = {"relpages", "reltuples", "relallvisible", "relfrozenxid", "relminmxid"}

_RELATIONS = """
    SELECT n.nspname, c.relname, c.oid, c.relispopulated,
           coalesce(array_agg(a.attname ORDER BY a.attnum) FILTER (WHERE a.attnum>0),
                    '{}'::name[]),
           coalesce(array_agg(a.attnum ORDER BY a.attnum) FILTER (WHERE a.attnum>0),
                    '{}'::smallint[]),
           coalesce(array_agg(a.atttypid IN ('aclitem'::regtype, 'aclitem[]'::regtype)
                     ORDER BY a.attnum) FILTER (WHERE a.attnum>0), '{}'::boolean[])
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
    WHERE c.relkind IN ('r','m','S')
    GROUP BY n.nspname,c.relname,c.oid,c.relispopulated ORDER BY n.nspname,c.relname
"""

# aclitem has no binary send function on PG16. Take the actual de-TOASTed ACL
# datum for this *visible* tuple, not a normalized list of grantees or SQL text.
# ctid is used only for lookup: it and the raw MVCC headers are not user fields.
_ACL = sql.SQL("""
    CASE WHEN {attr} IS NULL THEN NULL::bytea ELSE (
        SELECT (clinic_heap_inspect.tuple_data_split(
                    {oid}::oid, i.t_data, i.t_infomask, i.t_infomask2, i.t_bits, true
               ))[{slot}]
        FROM clinic_heap_inspect.heap_page_items(clinic_heap_inspect.get_raw_page(
            {relation}, split_part(substr(r.ctid::text,2),',',1)::bigint)) i
        WHERE i.lp=split_part(trim(both '()' from r.ctid::text),',',2)::integer
    ) END
""")
_ROWS = sql.SQL("""
    SELECT coalesce(string_agg(int4send(octet_length(wire))||wire, ''::bytea
                               ORDER BY wire), ''::bytea)
    FROM (SELECT record_send(ROW({cols})) AS wire FROM ONLY {relation} AS r) rows
""")


def logical_state(db: DatabaseWrapper) -> dict[str, bytes]:
    """All user values, sequence state and stored catalog definitions, not hashes."""
    name = db.settings_dict["NAME"]
    assert re.fullmatch(r"mig_resume_[0-9a-f]{12}", name)
    dsn = database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], name)
    state = {}
    with psycopg.connect(dsn) as cn:
        cn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cn.execute("SET LOCAL search_path=pg_catalog")
        for ns, table, oid, populated, names, slots, acl in cn.execute(_RELATIONS):
            if ns == "pg_catalog" and table in _STATISTICS:
                continue
            relation = sql.Identifier(ns, table)
            key = relation.as_string(cn)
            if not populated:  # An unpopulated materialized view cannot be scanned.
                state[key] = b""
                continue
            fields: list[sql.Composable] = []
            for col, num, is_acl in zip(names, slots, acl, strict=True):
                if ns == "pg_catalog" and table == "pg_class" and col in _ESTIMATES:
                    continue
                attr = sql.Identifier("r", col)
                fields.append(
                    _ACL.format(attr=attr, oid=oid, slot=num, relation=key)
                    if is_acl
                    else attr
                )
            row = cn.execute(
                _ROWS.format(cols=sql.SQL(",").join(fields), relation=relation)
            ).fetchone()
            assert row is not None
            state[key] = bytes(row[0])
    return state


def assert_unchanged(before: dict[str, bytes], after: dict[str, bytes]) -> None:
    changed = sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
    )
    assert not changed, f"Refusal changed logical state: {', '.join(changed)}"


@contextmanager
def observe_resume_sql(db: DatabaseWrapper) -> Iterator[list[str]]:
    """Observe successful PostgreSQL command tags without altering the transaction."""
    writes: list[str] = []

    def trace(
        run: Callable[..., object],
        statement: object,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        result = run(statement, params, many, context)
        cursor = cast("CursorWrapper", context["cursor"])
        tag = re.sub(r" [0-9 ]+$", "", cursor.cursor.statusmessage or "")
        if tag not in {
            "SELECT",
            "SHOW",
            "SET",
            "LOCK TABLE",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
        }:
            writes.append(tag)
        return result

    with db.execute_wrapper(trace):
        yield writes
