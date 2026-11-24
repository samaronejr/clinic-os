"""Migration-owner evidence for fast defaults; never learn from a cleared flag."""

from __future__ import annotations

import json
import struct
from contextlib import contextmanager
from typing import TYPE_CHECKING, NamedTuple

from django.db import transaction
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.utils import CursorWrapper


class ResumeCatalogError(RuntimeError):
    """The migration does not have sufficient evidence to proceed."""


class Column(NamedTuple):
    table: str
    relid: int
    name: str
    slot: int
    type_name: str
    type_oid: int
    typmod: int
    send_schema: str
    send_name: str
    keys: list[str]
    supported: bool
    blocks: int
    side: bytes | None

    @property
    def relation(self) -> sql.Identifier:
        return sql.Identifier("clinic_app", self.table)

    @property
    def key(self) -> sql.Composed:
        return sql.SQL("pg_catalog.record_send(ROW({}))").format(
            sql.SQL(",").join(sql.Identifier("r", k) for k in self.keys)
        )


def _refuse(table: str, column: str, why: str) -> ResumeCatalogError:
    return ResumeCatalogError(
        f"clinic_app.{table}.{column}.attmissingval: {why}; "
        "automatic recovery is impossible; restore or rebuild is required"
    )


def missing(table: str, column: str) -> ResumeCatalogError:
    return _refuse(table, column, "trusted migration witness is missing")


def lock_scope(db: BaseDatabaseWrapper, scope: Collection[str]) -> None:
    """Stabilize tuple locations and visible versions until this DDL commits."""
    names = [n.removeprefix("relation ") for n in scope if n.startswith("relation ")]
    with db.cursor() as cur:
        cur.execute(
            "SELECT relname FROM pg_catalog.pg_class WHERE "
            "relnamespace='clinic_app'::regnamespace AND relkind IN ('r','p') "
            "AND relname=ANY(%s) ORDER BY relname",
            [names],
        )
        tables = [
            sql.SQL("ONLY {}").format(sql.Identifier("clinic_app", t))
            for (t,) in cur.fetchall()
        ]
        if tables:
            cur.execute(
                sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.SQL(",").join(tables))
            )


def _columns(
    db: BaseDatabaseWrapper, tables: list[str], specific: str | None = None
) -> list[Column]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,c.oid,a.attname,a.attnum,
                quote_ident(tn.nspname)||'.'||quote_ident(t.typname),t.oid,
                a.atttypmod,pn.nspname,p.proname,
                ARRAY(SELECT k.attname FROM pg_catalog.pg_index i
                    CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY pos(num,idx)
                    JOIN pg_catalog.pg_attribute k ON k.attnum=pos.num
                    WHERE i.indrelid=c.oid AND i.indisprimary AND k.attrelid=c.oid
                      AND pos.idx<=i.indnkeyatts ORDER BY pos.idx),
                c.relkind='r' AND am.amname='heap'
                    AND c.relowner='clinic_owner'::regrole,
                pg_catalog.pg_relation_size(c.oid)/current_setting('block_size')::int,
                pg_catalog.array_send(a.attmissingval)
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
            JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
            JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
            LEFT JOIN pg_catalog.pg_proc p ON p.oid=t.typsend
            LEFT JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace
            LEFT JOIN pg_catalog.pg_am am ON am.oid=c.relam
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND a.attnum>0 AND NOT a.attisdropped
              AND (CASE WHEN %s::text IS NULL THEN a.atthasmissing
                        ELSE a.attname=%s END)
            ORDER BY c.relname,a.attnum
            """,
            [tables, specific, specific],
        )
        return [Column(*row) for row in cur.fetchall()]


@contextmanager
def _resolver_read(db: BaseDatabaseWrapper, col: Column) -> Iterator[CursorWrapper]:
    """Rollback the temporary SELECT grant and role, including their exact ACLs."""
    if not db.in_atomic_block:
        msg = "Migration witness requires the protected DDL transaction"
        raise ResumeCatalogError(msg)
    with transaction.atomic(using=db.alias), db.cursor() as cur:
        cur.execute(
            sql.SQL("GRANT SELECT ON {} TO clinic_resolver").format(col.relation)
        )
        cur.execute("SET LOCAL ROLE clinic_resolver")
        try:
            yield cur
        finally:
            transaction.set_rollback(True, using=db.alias)


def _require_store(db: BaseDatabaseWrapper, table: str, column: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('clinic_app.migration_default_witness') IS NOT NULL "
            "AND to_regclass('clinic_app.migration_default_witness_key') IS NOT NULL"
        )
        if not cur.fetchone()[0]:
            raise missing(table, column)


def _supported(col: Column) -> None:
    if not col.supported or not col.keys or not col.send_name:
        raise _refuse(col.table, col.name, "cannot prove a complete keyed heap cohort")


_BLOCKS = 128


def capture(db: BaseDatabaseWrapper, migration: str, scope: Collection[str]) -> None:
    """Capture once from a validated, still-present value in this migration."""
    if not db.in_atomic_block:
        msg = "Cannot write a migration witness outside the protected DDL transaction"
        raise ResumeCatalogError(msg)
    tables = [n.removeprefix("relation ") for n in scope if n.startswith("relation ")]
    for col in _columns(db, tables):
        _supported(col)
        _require_store(db, col.table, col.name)
        identity = (migration, col.table, col.name)
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO clinic_app.migration_default_witness "
                "(migration,relation_name,column_name,column_type,"
                "type_modifier,side_value,key_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,0) ON CONFLICT DO NOTHING RETURNING 1",
                [*identity, col.type_name, col.typmod, col.side],
            )
            if not cur.fetchone():
                continue
            count = 0
            for block in range(0, col.blocks, _BLOCKS):
                with _resolver_read(db, col) as source:
                    source.execute(
                        sql.SQL(
                            "SELECT {} FROM ONLY {} r "
                            "JOIN clinic_heap_inspect.short_tuples("
                            "%s::regclass,%s,%s) h ON h.location=r.ctid "
                            "WHERE h.natts<%s"
                        ).format(col.key, col.relation),
                        [col.relation.as_string(), block, block + _BLOCKS, col.slot],
                    )
                    keys = [k for (k,) in source.fetchall()]
                if keys:
                    cur.execute(
                        "INSERT INTO clinic_app.migration_default_witness_key "
                        "SELECT %s,%s,%s,unnest(%s::bytea[])",
                        [*identity, keys],
                    )
                    count += len(keys)
            cur.execute(
                "UPDATE clinic_app.migration_default_witness SET key_count=%s "
                "WHERE (migration,relation_name,column_name)=(%s,%s,%s)",
                [count, *identity],
            )


def verify(
    db: BaseDatabaseWrapper, migration: str, table: str, column: str, expected: str
) -> None:
    """Prove all cohorts for this physical column before publishing its fact."""
    _require_store(db, table, column)
    (col,) = _columns(db, [table], column)
    _supported(col)
    with db.cursor() as cur:
        cur.execute(
            "SELECT migration,column_type,type_modifier,side_value,key_count,"
            "verified_relid,verified_attnum "
            "FROM clinic_app.migration_default_witness "
            "WHERE (relation_name,column_name)=(%s,%s) AND "
            "(migration=%s OR (verified_relid,verified_attnum)=(%s,%s)) "
            "ORDER BY migration",
            [table, column, migration, col.relid, col.slot],
        )
        sources = cur.fetchall()
    if not sources:
        raise missing(table, column)
    # The OTP marker allows installation-specific instants in the reference,
    # not disagreements between two trusted witnesses on this installation.
    literal = json.loads(expected)
    side = bytes(sources[0][3])
    if isinstance(literal, str) and side.hex() != literal:
        raise _refuse(table, column, "witness default differs from the frozen column")
    element = _element(col, side)
    for origin, kind, typmod, value, count, relid, slot in sources:
        if (
            (kind, typmod) != (col.type_name, col.typmod)
            or bytes(value) != side
            or (relid, slot) not in {(None, None), (col.relid, col.slot)}
        ):
            raise _refuse(table, column, "witness does not identify this typed column")
        _verify_keys(db, col, origin, count, element)
    # No empty replacement cohort: subsequent migrations must reread the
    # original keys. This update rolls back with the containing protected DDL.
    with db.cursor() as cur:
        cur.execute(
            "UPDATE clinic_app.migration_default_witness "
            "SET verified_relid=%s,verified_attnum=%s "
            "WHERE (migration,relation_name,column_name)=(%s,%s,%s) "
            "AND verified_relid IS NULL AND verified_attnum IS NULL",
            [col.relid, col.slot, migration, table, column],
        )


def _verify_keys(
    db: BaseDatabaseWrapper, col: Column, origin: str, count: int, element: bytes
) -> None:
    with _resolver_read(db, col) as cur:
        cur.execute(
            sql.SQL(
                "SELECT count(*)=%s AND count(*) FILTER (WHERE r.tableoid IS NULL "
                "OR {}(r.{}) IS DISTINCT FROM %s::bytea)=0 "
                "FROM clinic_app.migration_default_witness_key k "
                "LEFT JOIN ONLY {} r ON {}=k.row_key "
                "WHERE (k.migration,k.relation_name,k.column_name)=(%s,%s,%s)"
            ).format(
                sql.Identifier(col.send_schema, col.send_name),
                sql.Identifier(col.name),
                col.relation,
                col.key,
            ),
            [count, element, origin, col.table, col.name],
        )
        if not cur.fetchone()[0]:
            raise _refuse(
                col.table, col.name, "witnessed row is missing or no longer holds V"
            )


def _element(col: Column, side: bytes) -> bytes:
    # PostgreSQL's array_send header: one dimension, no nulls, element type,
    # dimension length and lower bound, followed by the element's wire length.
    # Do not flatten an array-valued element or coerce a numeric's scale.
    header = struct.Struct("!6I")
    payload = side[header.size :]
    if side[: header.size] != header.pack(1, 0, col.type_oid, 1, 1, len(payload)):
        raise _refuse(col.table, col.name, "witness has an invalid typed default")
    return payload
