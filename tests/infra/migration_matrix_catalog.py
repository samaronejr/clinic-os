"""Independent schema oracle and real PostgreSQL mutations for recovery tests."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from hashlib import sha256
from typing import TYPE_CHECKING

import psycopg
from ops.testing.restore_container import ContainerPostgres
from psycopg import sql

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from django.db.backends.postgresql.base import DatabaseWrapper


def schema_hash(db: DatabaseWrapper) -> str:
    cfg = db.settings_dict
    args = (
        "--schema-only",
        "--schema=clinic_app",
        "--no-password",
        "--username=clinic_owner",
        f"--dbname={cfg['NAME']}",
    )
    if client := shutil.which("pg_dump"):
        # Fixed read-only operation; no password in the argv, inherited
        # environment or error message. Use the connection's actual endpoint.
        raw = subprocess.check_output(  # noqa: S603
            [client, *args, f"--host={cfg['HOST']}", f"--port={cfg['PORT']}"],
            env={"PGPASSWORD": cfg["PASSWORD"], "LC_ALL": "C"},
            timeout=120,
        )
    else:
        container = ContainerPostgres(
            os.environ["POSTGRES_CONTAINER"],
            cfg["NAME"],
            cfg["PASSWORD"],
            role="clinic_owner",
        )
        raw = container.execute(
            (
                "pg_dump",
                *args,
                "--host=127.0.0.1",
                "--port=" + os.environ["POSTGRES_PORT"],
            )
        )
    # PG16's session-random psql guard is not part of the schema.
    return sha256(
        b"\n".join(
            line
            for line in raw.splitlines()
            if not line.startswith((b"\\restrict ", b"\\unrestrict "))
        )
    ).hexdigest()


def heaps(db: DatabaseWrapper, scope: list[str]) -> list[tuple[str, str | None]]:
    """The live cut determines which written heaps and clustering indexes exist."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, (
                SELECT idx.relname FROM pg_index i
                JOIN pg_class idx ON idx.oid=i.indexrelid
                WHERE i.indrelid=c.oid AND i.indisvalid AND i.indisready AND i.indislive
                  AND i.indpred IS NULL AND pg_index_has_property(idx.oid,'clusterable')
                ORDER BY i.indisclustered DESC, i.indisprimary DESC,
                         idx.relname COLLATE "C"
                LIMIT 1)
            FROM pg_class c WHERE c.relnamespace='clinic_app'::regnamespace
              AND c.relkind='r' AND c.relname=ANY(%s)
            ORDER BY c.relname COLLATE "C"
        """,
            [scope],
        )
        return list(cur.fetchall())


def maintain(db: DatabaseWrapper, scope: list[str], operation: str) -> list[str]:
    statements = {
        "ANALYZE": sql.SQL("ANALYZE {}"),
        "REINDEX": sql.SQL("REINDEX TABLE {}"),
        "VACUUM": sql.SQL("VACUUM FULL {}"),
    }
    targets = heaps(db, scope)
    assert targets, f"No live heaps at the cut in {scope}"
    applied = []
    with db.cursor() as cur:
        for name, idx in targets:
            table = sql.Identifier("clinic_app", name)
            if operation == "CLUSTER":
                if idx is None:  # No eligible index; do not invent one.
                    continue
                cur.execute(
                    sql.SQL("CLUSTER {} USING {}").format(table, sql.Identifier(idx))
                )
            else:
                cur.execute(statements[operation].format(table))
            applied.append(name)
    return applied


def _change_default(db: DatabaseWrapper, names: list[str]) -> str:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,a.attname FROM pg_attribute a
            JOIN pg_class c ON c.oid=a.attrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND a.attnum>0 AND NOT a.attisdropped AND a.atthasdef
              AND a.attidentity='' AND a.attgenerated=''
            ORDER BY c.relname COLLATE "C",a.attnum LIMIT 1
        """,
            [names],
        )
        row = cur.fetchone()
        assert row, f"No declared default in {names}"
        table, col = row
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER {} DROP DEFAULT").format(
                sql.Identifier("clinic_app", table), sql.Identifier(col)
            )
        )
        return f"{table}.{col}"


def _change_trigger(db: DatabaseWrapper, names: list[str]) -> str:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,t.tgname,t.tgenabled FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            ORDER BY t.tgisinternal,c.relname COLLATE "C",t.tgname COLLATE "C" LIMIT 1
        """,
            [names],
        )
        row = cur.fetchone()
        assert row, f"No trigger in {names}"
        table, name, enabled = row
        assert isinstance(table, str)
    # A scratch administrator can change an internal RI trigger. Retry always
    # reconnects as the ordinary migration owner, never the administrator.
    admin = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], db.settings_dict["NAME"]
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER TABLE {} {} TRIGGER {}").format(
                sql.Identifier("clinic_app", table),
                sql.SQL("ENABLE") if enabled == "D" else sql.SQL("DISABLE"),
                sql.Identifier(name),
            )
        )
    return table


def _invalidate_index(db: DatabaseWrapper, names: list[str]) -> str:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,ix.relname,pg_get_indexdef(i.indexrelid)
            FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
            JOIN pg_class ix ON ix.oid=i.indexrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND c.relkind='r' AND ix.relkind='i' AND i.indisvalid AND i.indisready
              AND i.indislive AND NOT i.indisreplident AND NOT i.indisclustered
              AND NOT EXISTS (SELECT FROM pg_constraint WHERE conindid=i.indexrelid)
              AND NOT EXISTS (SELECT FROM pg_inherits WHERE inhrelid=ix.oid)
            ORDER BY c.relname COLLATE "C",ix.relname COLLATE "C" LIMIT 1
        """,
            [names],
        )
        row = cur.fetchone()
        assert row, f"No standalone index in {names}"
        table, index, definition = row
        cur.execute(
            sql.SQL("DROP INDEX {}").format(sql.Identifier("clinic_app", index))
        )
    admin = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], db.settings_dict["NAME"]
    )
    concurrent, changed = re.subn(
        r"\ACREATE (UNIQUE )?INDEX ",
        r"CREATE \1INDEX CONCURRENTLY ",
        definition,
        count=1,
    )
    assert changed == 1
    # CREATE INDEX CONCURRENTLY first commits the invalid catalog stub and
    # then waits for writers. A held writer lock guarantees the timeout, not
    # a race or a direct UPDATE of pg_index.
    with (
        psycopg.connect(admin) as writer,
        psycopg.connect(admin, autocommit=True) as builder,
    ):
        writer.execute(
            sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(
                sql.Identifier("clinic_app", table)
            )
        )
        builder.execute("SET ROLE clinic_owner")
        builder.execute("SET search_path=clinic_app,public")
        builder.execute("SET lock_timeout='250ms'")
        try:
            builder.execute(sql.SQL(concurrent))
        except psycopg.errors.LockNotAvailable:
            pass
        else:
            msg = "Concurrent index build did not stop behind its writer"
            raise AssertionError(msg)
    with db.cursor() as cur:
        cur.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid=%s::regclass",
            [sql.Identifier("clinic_app", index).as_string()],
        )
        assert cur.fetchone() == (False,)
    return f"{table}.{index}"


def _plant_side_value(db: DatabaseWrapper, names: list[str]) -> str:
    # Recreate an existing, last, unconstrained column with the same declared
    # type and SQL default. Only its historical substitution should differ.
    choices = {
        "bool": ("false", "true"),
        "int2": ("17", "18"),
        "int4": ("17", "18"),
        "int8": ("17", "18"),
        "text": ("a", "b"),
        "varchar": ("a", "b"),
        "bytea": ("\\x61", "\\x62"),
        "jsonb": ("17", "18"),
        "timestamptz": ("2001-01-01 00:00:00+00", "infinity"),
    }
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,a.attname,t.typname,format_type(t.oid,a.atttypmod),
                   a.attnotnull,pg_get_expr(d.adbin,d.adrelid),array_send(a.attmissingval)
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_type t ON t.oid=a.atttypid
            LEFT JOIN pg_attrdef d ON (d.adrelid,d.adnum)=(a.attrelid,a.attnum)
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
              AND a.attidentity='' AND a.attgenerated='' AND a.attacl IS NULL
              AND t.typnamespace='pg_catalog'::regnamespace AND t.typname=ANY(%s)
              AND a.attcollation=t.typcollation AND a.attstorage=t.typstorage
              AND NOT EXISTS (
                  SELECT FROM pg_inherits WHERE c.oid IN (inhparent,inhrelid))
              AND NOT EXISTS (SELECT FROM pg_attribute b WHERE b.attrelid=c.oid
                              AND b.attnum>a.attnum AND NOT b.attisdropped)
              AND NOT EXISTS (SELECT FROM pg_depend p
                  WHERE p.refclassid='pg_class'::regclass
                  AND p.refobjid=c.oid AND p.refobjsubid=a.attnum
                  AND NOT (p.classid='pg_attrdef'::regclass AND p.objid=d.oid IS TRUE))
            ORDER BY a.atthasmissing DESC,c.relname COLLATE "C" LIMIT 1
        """,
            [names, list(choices)],
        )
        row = cur.fetchone()
        assert row, f"No independent last column for a fast-default mutation in {names}"
        name, col, typ, kind, notnull, declared, side = row
        table = sql.Identifier("clinic_app", name)
        column = sql.Identifier(col)
        value = choices[typ][0]
        cur.execute(
            sql.SQL("SELECT array_send(ARRAY[%s::{}])").format(sql.SQL(kind)), [value]
        )
        if cur.fetchone() == (side,):
            value = choices[typ][1]
        cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(table, column))
        cur.execute(
            sql.SQL("ALTER TABLE {} ADD COLUMN {} {} {} DEFAULT {}").format(
                table,
                column,
                sql.SQL(kind),
                sql.SQL("NOT NULL" if notnull else ""),
                sql.Literal(value),
            )
        )
        default = (
            sql.SQL("SET DEFAULT {}").format(sql.SQL(declared))
            if declared is not None
            else sql.SQL("DROP DEFAULT")
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER COLUMN {} {}").format(table, column, default)
        )
        cur.execute(
            "SELECT atthasmissing,array_send(attmissingval) FROM pg_attribute "
            "WHERE attrelid=%s::regclass AND attname=%s",
            [table.as_string(), col],
        )
        present, planted = cur.fetchone()
        assert present
        assert planted is not None
        assert planted != side
        return f"{name}.{col}"


TAMPERS = {
    "side-value": _plant_side_value,
    "declared-default": _change_default,
    "trigger": _change_trigger,
    "invalid-index": _invalidate_index,
}
